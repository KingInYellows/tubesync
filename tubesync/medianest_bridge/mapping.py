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
from datetime import timezone as dt_timezone
from functools import reduce
from operator import or_ as _or_

from django.db.models import Q
from django.utils import timezone

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
    # Matches views.py's _now_iso() formatting (millisecond precision,
    # literal Z) -- plain .isoformat() emits microseconds and a +00:00
    # offset instead, which fails the contract's own '*At' field pattern
    # now that test_endpoints.py's assert_matches_schema() enforces it
    # for every field ending in "At" (merged in from the foundation
    # branch's T1 work).
    if not value:
        return None
    return value.astimezone(dt_timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


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


def _task_has_override(task):
    '''
        True when this download_media_file task was scheduled with
        override=True -- as sync/views/media.py's MediaRedownloadView
        does. download_media_file(media_id, override=False) forwards
        override straight into Media.download_checklist(skip_checks=...),
        bypassing its media.skip / source.download_media rejection
        entirely, so a task carrying override=True is not a no-op even
        while skip/ineligible currently applies, and must not be
        suppressed by _is_stale_given_current_media_state() below.

        task_params[1] is repr(task_obj.kwargs) (common/models/tasks.py
        ::th_schedule) -- a string, not a real dict -- so this is a
        string check, matching how this codebase already keys off
        task_params elsewhere (e.g. the istartswith prefix match in
        batch_media_download_tasks()).
    '''
    if not task or len(task.task_params) < 2:
        return False
    return "'override': True" in (task.task_params[1] or '')


def _task_failure_is_current(task):
    '''
        historical_task() (common/huey.py) sets failed_at and last_error
        only inside its `elif exception_obj is not None` branch, and
        never clears either one afterward -- including on a later
        success. end_at, by contrast, is touched unconditionally by
        every signal the row receives (`th.end_at = signal_dt`,
        unconditional at the end of that function), and both failed_at
        and end_at are set from the SAME signal_dt within a single
        SIGNAL_ERROR call. So failed_at == end_at means the failure was
        this row's most recent signal; failed_at < end_at means at least
        one later signal happened since -- a subsequent SIGNAL_COMPLETE
        (huey's retries= mechanism reuses the same task_id/row on retry,
        per the retryAt docstring below, so a since-succeeded retry
        keeps the old failed_at forever without this check) or a fresh
        SIGNAL_EXECUTING for another retry attempt. Either way, the
        failure is no longer this row's current state (P2 review
        finding: a media item that failed once and later succeeded on
        retry kept reporting a stale error indefinitely).

        That equality check alone is too strict, though (P2 review
        finding, fresh evidence beyond the above): huey's own retry
        mechanism re-enqueues the SAME row via a SIGNAL_SCHEDULED before
        the retry actually starts, and that branch updates scheduled_at
        to the future retry time without touching failed_at -- while
        end_at still advances (unconditionally, as above). So
        failed_at < end_at also occurs the moment a retry is merely
        scheduled, well before it starts or resolves, and the plain
        equality check would misreport that still-failed, retry-pending
        row as no longer failed. scheduled_at is the same signal
        retryAt's own gate below already uses to mean "a retry is
        genuinely still pending": SIGNAL_EXECUTING/SIGNAL_COMPLETE never
        push it forward again once the retry starts, so it only reads
        as still-in-the-future during this exact window. A failure is
        therefore still current either when it's the row's latest
        signal outright, or when a later SCHEDULED signal moved end_at
        forward but the resulting retry hasn't started yet.
    '''
    if not task or task.failed_at is None:
        return False
    if task.failed_at == task.end_at:
        return True
    return task.scheduled_at is not None and task.scheduled_at > timezone.now()


def _is_stale_given_current_media_state(media, task, *, is_running):
    '''
        True when `task` no longer represents media's actual current
        fate, because a later, explicit signal (skip, or the source
        having download_media turned off) makes it irrelevant --
        regardless of whether the task already failed or is merely still
        pending. Never true for a currently-running task: an active
        download always wins, matching get_download_state()'s own
        precedence.

        Covers two review findings:
          - A terminal failure kept reporting "failed" after the fact
            (get_download_state()'s skip/source.download_media checks
            are unreachable whenever any task is passed in at all).
          - A merely-pending (not yet started, not failed) task kept
            reporting "queued" after the fact, even though
            sync/models/media__tasks.py's download_checklist() will
            reject it as a no-op the moment it actually starts (media.skip
            or not source.download_media) -- UNLESS it carries
            override=True, which bypasses that rejection, so such a task
            is excluded from this suppression via _task_has_override().
    '''
    if not task or is_running:
        return False
    if not (media.skip or not media.source.download_media):
        return False
    return not _task_has_override(task)


def serialize_media(media, *, download_task=_MISSING):
    if download_task is _MISSING:
        task = get_relevant_media_download_task(str(media.pk))
    else:
        task = download_task or False

    is_running = bool(task) and getattr(task, 'locked_by_pid_running', lambda: False)()
    if _is_stale_given_current_media_state(media, task, is_running=is_running):
        task = False

    raw_state = media.get_download_state(task or None)
    has_error = _task_failure_is_current(task)

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
        # sets no retries=, so its own scheduled_at never gets pushed
        # forward on failure -- but sync/views/media.py's
        # MediaRedownloadView explicitly passes retries=3, retry_delay=600
        # when scheduling a manual redownload, and huey's own retry
        # mechanism reschedules the SAME row (same task_id) to a genuine
        # future scheduled_at on that path (common/huey.py's
        # historical_task updates scheduled_at again on the subsequent
        # SCHEDULED signal, without clearing the prior failed_at/
        # last_error the ERROR signal already set). So "does this row
        # still have a pending retry" is exactly "is its own scheduled_at
        # still in the future" -- true for a mid-retry manual redownload,
        # false for an exhausted/never-configured-to-retry failure, where
        # scheduled_at is stuck at whenever that attempt itself ran.
        'retryAt': (
            _iso(task.scheduled_at)
            if (task and has_error and task.scheduled_at > timezone.now())
            else None
        ),
        'error': get_error_message(task) if (task and has_error) else None,
    }
