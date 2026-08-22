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
from sync.choices import MediaState, Val, YouTube_SourceType
from sync.tasks import get_error_message, get_media_download_task, get_source_index_task

_MISSING = object()


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
        Return {media_id_str: task_or_False} for a page of media rows.
        Uses the same running-task predicate as get_media_download_task()
        (contract: bridge-openapi.v1.yaml listSourceMedia description).
    '''
    id_set = {str(media_id) for media_id in media_ids}
    if not id_set:
        return {}
    from sync.tasks import get_running_tasks
    result = {media_id: False for media_id in id_set}
    tqs = get_running_tasks().filter(name__endswith='download_media_file')
    for task in tqs:
        params = task.task_params
        if not params or not params[0]:
            continue
        media_id = str(params[0][0])
        if media_id in id_set:
            result[media_id] = task
    return result


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
        task = get_media_download_task(str(media.pk))
    else:
        task = download_task or False
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
        'retryAt': _iso(task.scheduled_at) if (task and has_error) else None,
        'error': get_error_message(task) if (task and has_error) else None,
    }
