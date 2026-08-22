'''
    Pure functions mapping TubeSync's sync.models.Source/Media rows to the
    vendored contract's Source/MediaItem response schemas. No Django view,
    URL, or auth concerns live here -- these are read-only ORM->dict
    transforms, testable in isolation.

    Every mapping decision that is not a direct field rename is documented
    below with its reasoning; the contract itself is silent on the exact
    TubeSync-state-to-normalized-state precedence, so these are this app's
    own considered choices, not something guessed silently.
'''
from functools import reduce
from operator import or_ as _or_

from django.db.models import Q

from common.models import TaskHistory
from sync.choices import MediaState, Val, YouTube_SourceType
from sync.tasks import get_error_message, get_running_tasks, get_source_index_task

_MISSING = object()

# Set by sync/views/tasks.py's RevokeTaskView (and mirrored by
# medianest_bridge/sync_dedup.py's own revoke path) on a cancelled
# scheduled-but-never-started task: start_at stays NULL, so without this
# exclusion a revoked row would otherwise still match the pending/failed
# fallback tier below and be reported as "queued" forever.
_REVOKED_VERBOSE_PREFIX = '[revoked] '


def _iso(value):
    return value.isoformat() if value else None


def map_source_type(raw_source_type):
    '''
        TubeSync's Source.source_type has three values (YouTube_SourceType):
        'c' (channel by handle/name), 'i' (channel by ID), 'p' (playlist).
        The contract's sourceType enum only distinguishes channel/playlist
        -- both 'c' and 'i' are channel variants from the contract's point
        of view, so both map to "channel".
    '''
    if raw_source_type == Val(YouTube_SourceType.PLAYLIST):
        return 'playlist'
    return 'channel'


def map_source_state(source, *, index_task=None):
    '''
        Precedence, most-actionable-first (documented, not contract-defined
        -- see this module's docstring):

        1. has_failed=True -> "failed", regardless of anything else; a
           failed index is the most actionable signal a caller needs.
        2. An index_source task is currently running for this source
           (sync.tasks.get_source_index_task, the same helper
           SourceSyncNowView's dedup logic would use) -> "syncing".
        3. Not source.is_active (index_schedule is NEVER, or none of
           download_media/index_streams/index_videos are enabled) ->
           "disabled".
        4. is_active but last_crawl is still None (created, first index
           hasn't completed yet) -> "provisioning".
        5. is_active, has crawled at least once, not failed, not currently
           syncing -> "active".

        "unknown" is never emitted by this function -- the above is
        exhaustive over is_active/has_failed/last_crawl/task-running, all
        of which are always determinable for an existing Source row.

        index_task, when supplied, is a snapshot from a single
        get_source_index_task() call shared with rawState.indexTaskRunning
        so both fields cannot disagree if the task starts or finishes
        between lookups.
    '''
    if source.has_failed:
        return 'failed'
    if index_task is None:
        index_task = get_source_index_task(str(source.pk))
    if index_task:
        return 'syncing'
    if not source.is_active:
        return 'disabled'
    if source.last_crawl is None:
        return 'provisioning'
    return 'active'


def serialize_source(source):
    index_task = get_source_index_task(str(source.pk))
    return {
        'uuid': str(source.pk),
        'sourceType': map_source_type(source.source_type),
        'canonicalKey': source.key,
        'canonicalUrl': source.url,
        'name': source.name,
        'directory': source.directory,
        'rawState': {
            'hasFailed': source.has_failed,
            'isActive': source.is_active,
            'lastCrawl': _iso(source.last_crawl),
            'indexTaskRunning': bool(index_task),
        },
        'normalizedState': map_source_state(source, index_task=index_task),
        'lastCrawlAt': _iso(source.last_crawl),
    }


def batch_media_download_tasks(media_ids):
    '''
        Return {media_id_str: task_or_False} for a page of media rows: the
        download_media_file TaskHistory row that best represents each
        media's current state, in two tiers matching
        get_download_state()'s own DOWNLOADING > ERROR > SCHEDULED
        precedence:

          1. A currently-running task (get_running_tasks(), the same
             upstream-consistent predicate get_media_download_task() uses).
          2. For any media not caught by (1): the most recently scheduled
             task that has NOT completed successfully and is not marked
             [revoked] -- either a failure (failed_at set) or one that
             has not started yet (start_at is still null, i.e.
             delayed/queued). A row cancelled through
             sync/views/tasks.py's RevokeTaskView also has start_at still
             NULL, but is excluded by its [revoked] verbose_name prefix
             so it is never reported as "queued" (it will never run).
             Media with only a plain successful task row, or no task row
             at all, correctly stay False here -- Media.downloaded
             already short-circuits get_download_state() before any task
             is consulted in that case, so a successful row is never
             relevant to this lookup.

        Before this, callers relying solely on the running-task predicate
        (upstream's get_media_download_task()) had a delayed-but-not-yet-
        started task and a terminal failure both silently disappear, and
        get_download_state() would then fall through to "discovered"
        instead of the correct "queued"/"failed" (T2 review P1 finding).

        Each returned task also has locked_by_pid_running bound (see
        _bind_known_running_state below) so serialize_media()'s call into
        Media.get_download_state(task) takes that fast, already-known
        branch instead of re-querying per row (T2 review P2 finding: a
        plain TaskHistory instance has no locked_by_pid_running of its
        own, so get_download_state()'s internal "is this still running"
        recheck otherwise falls back to calling get_media_download_task()
        again for every non-idle row on the page, even after batching).
    '''
    id_set = {str(media_id) for media_id in media_ids}
    if not id_set:
        return {}
    result = {media_id: False for media_id in id_set}

    running_qs = get_running_tasks().filter(name__endswith='download_media_file')
    for task in running_qs:
        params = task.task_params
        if not params or not params[0]:
            continue
        media_id = str(params[0][0])
        if media_id in id_set:
            _bind_known_running_state(task, is_running=True)
            result[media_id] = task

    remaining = {media_id for media_id in id_set if not result[media_id]}
    if remaining:
        # Constrain the fallback query to the requested media_ids at the
        # database level (the same task_params__istartswith prefix match
        # sync.tasks.get_model_tasks() uses for a single ID, OR'd across
        # this batch) -- without this, a page containing even one media
        # with no pending/failed task would iterate every such row across
        # the whole deployment's retained task history, since Python-side
        # filtering alone can never empty `remaining` in that case.
        id_filter = reduce(
            _or_,
            (Q(task_params__istartswith=f'[["{media_id}"') for media_id in remaining),
        )
        pending_or_failed_qs = (
            TaskHistory.objects
            .exclude(verbose_name__startswith=_REVOKED_VERBOSE_PREFIX)
            .filter(name__endswith='download_media_file')
            .filter(Q(failed_at__isnull=False) | Q(start_at__isnull=True))
            .filter(id_filter)
            .order_by('-scheduled_at')
        )
        for task in pending_or_failed_qs:
            params = task.task_params
            if not params or not params[0]:
                continue
            media_id = str(params[0][0])
            if media_id in remaining:
                _bind_known_running_state(task, is_running=False)
                result[media_id] = task
                remaining.discard(media_id)
                if not remaining:
                    break
    return result


def _bind_known_running_state(task, *, is_running):
    '''
        Media.get_download_state(task) checks
        hasattr(task, 'locked_by_pid_running') to decide whether it can
        trust the task object's own answer or must fall back to querying
        get_media_download_task() again -- a plain TaskHistory row never
        has that attribute (it belongs to huey's runtime Task/lock
        object, not the Django model), so every call previously took the
        query-again fallback. Since batch_media_download_tasks() already
        knows which tier (running vs. pending/failed) produced this task,
        binding a trivial callable here lets get_download_state() take
        its fast, already-known branch instead.
    '''
    task.locked_by_pid_running = lambda: is_running


def get_relevant_media_download_task(media_id):
    '''Single-item convenience wrapper around batch_media_download_tasks().'''
    return batch_media_download_tasks([media_id]).get(str(media_id), False)


# TubeSync's real MediaState -> the contract's normalizedState enum.
# UNKNOWN deliberately maps to "discovered", not "unknown": TubeSync's
# UNKNOWN is a well-determined state (indexed, not downloaded, not
# skipped, source not globally disabled -- i.e. sitting there with no
# work item yet), not a case this bridge cannot determine. The contract's
# "unknown" is reserved for the latter (see readiness.py's health
# components for the same distinction applied to T1). "processing" and
# "removed_upstream" are never emitted -- see the contract's own
# MediaItem.normalizedState description (processing has no separate
# TubeSync-observable signal; removed_upstream is reserved/unmapped
# pending the delete_removed_media follow-up noted in ADR-0006).
_STATE_MAP = {
    Val(MediaState.UNKNOWN): 'discovered',
    Val(MediaState.SCHEDULED): 'queued',
    Val(MediaState.DOWNLOADING): 'downloading',
    Val(MediaState.DOWNLOADED): 'downloaded',
    Val(MediaState.SKIPPED): 'skipped',
    Val(MediaState.DISABLED_AT_SOURCE): 'ineligible',
    Val(MediaState.ERROR): 'failed',
}


def map_media_state(raw_state):
    return _STATE_MAP.get(raw_state, 'unknown')


def serialize_media(media, *, download_task=_MISSING):
    if download_task is _MISSING:
        task = get_relevant_media_download_task(str(media.pk))
    else:
        task = download_task or False

    # A terminal failure must not keep reporting "failed" once a later,
    # explicit signal (skip, or the source having download_media turned
    # off) makes it irrelevant. get_download_state()'s own skip/
    # source.download_media checks are unreachable whenever any task is
    # passed in at all (its `if task:` branch always returns before
    # reaching them), so a failed-then-skipped media would otherwise
    # report "failed" until the TaskHistory row ages out via task-
    # history cleanup (normally up to 30 days). Safe to drop the task
    # here without risking masking an active download: has_error() and
    # "currently running" are mutually exclusive on the same row (see
    # get_download_state()'s own if running(task): ... elif
    # task.has_error(): ...), so a task with has_error() True is by
    # construction never the running one.
    if task and task.has_error() and (media.skip or not media.source.download_media):
        task = False

    raw_state = media.get_download_state(task or None)
    has_error = bool(task) and task.has_error()

    relative_path = None
    filename = None
    if media.downloaded and media.media_file:
        # media_file's storage root is settings.DOWNLOAD_ROOT
        # (sync/models/_migrations.py::media_file_storage), so .name is
        # already relative to the download root -- exactly what the
        # contract's relativePath requires.
        relative_path = media.media_file.name
        filename = relative_path.rsplit('/', 1)[-1] if relative_path else None

    return {
        'id': str(media.pk),
        'sourceId': str(media.source_id),
        'youtubeKey': media.key,
        'title': media.title,
        'publishedAt': _iso(media.published),
        'rawState': raw_state,
        'normalizedState': map_media_state(raw_state),
        'eligible': bool(media.can_download),
        'relativePath': relative_path,
        'filename': filename,
        'sizeBytes': media.downloaded_filesize,
        'downloadedAt': _iso(media.download_date),
        # download_media_file's own @db_task decorator (sync/tasks.py)
        # sets no retries= -- huey never automatically re-enqueues it on
        # failure, and nothing else in this codebase reschedules a failed
        # row either (confirmed: no retry/reschedule logic anywhere
        # touches a failed_at row for this task name). A failed row's own
        # scheduled_at is therefore always when THAT attempt ran, never a
        # future retry time, so retryAt is unconditionally null here --
        # not something this task type can ever have until upstream
        # actually adds a retry mechanism for it.
        'retryAt': None,
        'error': get_error_message(task) if (task and has_error) else None,
    }
