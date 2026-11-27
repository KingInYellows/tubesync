'''
    Contract 1.6.0 (MED-809, MED-810, MediaNest ADR-0036):
    PATCH /sources/{sourceUuid} (pause, resume, schedule) and
    POST /sources/{sourceUuid}/media/{mediaId}/retry|skip.

    Ordinary write routes: BridgeView.dispatch() applies the same gates as
    POST /sources (bearer token, CIDR, MEDIANEST_BRIDGE_READ_ONLY, body
    size) and writes the mutation audit line.

    Retry and skip do what TubeSync's own "redownload" and "skip" buttons
    do (sync/views/media.py MediaRedownloadView, MediaSkipView), minus the
    parts that touch files: neither route deletes or replaces a
    downloaded file, so both refuse a downloaded video (409
    MEDIA_DOWNLOADED).
'''
import uuid

from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from common.logger import log
from common.models import TaskHistory
from sync.models import Media
from sync.tasks import (
    download_media_metadata,
    get_model_tasks,
    refresh_formats,
    schedule_manual_media_download,
)

from . import deletion, mapping, pause
from .errors import error_response
from .request_schemas import validate_update_source_request
from .sync_dedup import _REVOKED_VERBOSE_PREFIX, _revoke_pending_index_task
from .videolist import is_videolist
from .views import BridgeView
from .views_delete import SourceDetailDeleteView
from .views_sources import _get_source_or_error, _source_not_found
from .views_write import _invalid, _parse_json_body, source_deleting


def _source_or_error(source_uuid, request_id):
    '''
        The live source, or the error for a write route: 409
        SOURCE_DELETING for a source being deleted (its uuid, or the hidden
        row the deletion leaves), 404 otherwise.
    '''
    source, error = _get_source_or_error(source_uuid, request_id)
    if error:
        if deletion.pending_deletion_for_uuid(source_uuid) is not None:
            return None, source_deleting(request_id)
        return None, error
    if deletion.is_pending_deletion(source):
        return None, _source_not_found(request_id)
    return source, None


class SourceDetailControlView(SourceDetailDeleteView):
    '''
        GET and DELETE /sources/{sourceUuid} (inherited) + PATCH, the
        contract's updateSource. Changes only UpdateSourceRequest's fields;
        see pause.py for what pausing does to the TubeSync source.
    '''

    def patch(self, request, source_uuid, *args, **kwargs):
        request_id = request._bridge_request_id
        body, error = _parse_json_body(request, request_id)
        if error:
            return error
        schema_errors = validate_update_source_request(body)
        if schema_errors:
            return _invalid(request_id, schema_errors)
        source, error = _source_or_error(source_uuid, request_id)
        if error:
            return error
        if is_videolist(source):
            return _invalid(request_id, [
                'A video list never indexes, so it cannot be paused or given a schedule.',
            ])
        updated = pause.apply_update(
            source.pk,
            paused=body.get('paused'),
            index_schedule=body.get('indexSchedule'),
        )
        log.info(
            'medianest_bridge: source updated source_uuid=%s fields=%s request_id=%s',
            source.pk, ','.join(sorted(body)), request_id,
        )
        return JsonResponse(mapping.serialize_source(updated))


def _media_or_error(source, media_id, request_id):
    try:
        parsed = uuid.UUID(str(media_id))
    except (ValueError, AttributeError, TypeError):
        parsed = None
    media = None
    if parsed is not None:
        media = Media.objects.select_related('source').filter(pk=parsed, source=source).first()
    if media is None:
        return None, error_response(
            status=404,
            code='MEDIA_NOT_FOUND',
            title='Video not found',
            detail='This source has no video with the given identifier.',
            request_id=request_id,
            retryable=False,
        )
    return media, None


def _media_downloaded(request_id):
    return error_response(
        status=409,
        code='MEDIA_DOWNLOADED',
        title='Video is already downloaded',
        detail='The video is already downloaded. This route never deletes or replaces a file.',
        request_id=request_id,
        retryable=False,
    )


def _running_download_task(media):
    task = mapping.get_relevant_media_download_task(str(media.pk))
    if task and getattr(task, 'locked_by_pid_running', lambda: False)():
        return task
    return None


def _media_response(media):
    fresh = Media.objects.select_related('source').get(pk=media.pk)
    return JsonResponse(mapping.serialize_media(fresh), status=202)


class _MediaControlView(BridgeView):

    def _load(self, request_id, source_uuid, media_id):
        source, error = _source_or_error(source_uuid, request_id)
        if error:
            return None, error
        media, error = _media_or_error(source, media_id, request_id)
        if error:
            return None, error
        return media, None


class RetryMediaView(_MediaControlView):
    '''
        POST /sources/{sourceUuid}/media/{mediaId}/retry -- retryMedia.
        Un-skips the video and queues a manual download, the way
        MediaRedownloadView does for a video with nothing on disk: fresh
        formats first (the usual cure for an expired-format 403), then
        schedule_manual_media_download() with TubeSync's own retries. A
        video with no metadata yet gets a manual metadata fetch, and that
        task queues the download itself once the metadata is in.
    '''

    def post(self, request, source_uuid, media_id, *args, **kwargs):
        request_id = request._bridge_request_id
        media, error = self._load(request_id, source_uuid, media_id)
        if error:
            return error
        if pause.is_paused(media.source):
            return error_response(
                status=409,
                code='SOURCE_PAUSED',
                title='Source is paused',
                detail='The source is paused. Resume it, then retry the video.',
                request_id=request_id,
                retryable=False,
            )
        if media.downloaded:
            return _media_downloaded(request_id)
        if _running_download_task(media):
            return _media_response(media)

        with transaction.atomic():
            media = Media.objects.select_for_update().select_related('source').get(pk=media.pk)
            if media.has_metadata:
                media.save_to_metadata('_refresh_formats_attempted', 1)
                media.save_to_metadata('formats_epoch', 1)
                TaskHistory.schedule(
                    refresh_formats,
                    str(media.pk),
                    priority=90,
                    remove_duplicates=False,
                    retries=1,
                    retry_delay=300,
                    vn_fmt=_('Refreshing formats (manually) for "{}"'),
                    vn_args=(media.key,),
                )
                schedule_manual_media_download(media)
            else:
                TaskHistory.schedule(
                    download_media_metadata,
                    str(media.pk),
                    manual=True,
                    download_after=True,
                    priority=90,
                    remove_duplicates=True,
                    vn_fmt=_('Downloading metadata (manually) for "{}": "{}"'),
                    vn_args=(media.key, media.name),
                )
            media.skip = False
            media.manual_skip = False
            media.save()
        log.info(
            'medianest_bridge: media retry media_id=%s source_uuid=%s request_id=%s',
            media.pk, media.source_id, request_id,
        )
        return _media_response(media)


class SkipMediaView(_MediaControlView):
    '''
        POST /sources/{sourceUuid}/media/{mediaId}/skip -- skipMedia. Sets
        skip and manual_skip, as MediaSkipView does: media_post_save()
        returns early for a manually skipped video, so no later index or
        filter change un-skips it. Queued downloads are revoked too, since
        a download a retry queued carries override=True, which would
        otherwise download a skipped video anyway.
    '''

    def post(self, request, source_uuid, media_id, *args, **kwargs):
        request_id = request._bridge_request_id
        media, error = self._load(request_id, source_uuid, media_id)
        if error:
            return error
        if media.downloaded:
            return _media_downloaded(request_id)
        if _running_download_task(media):
            return error_response(
                status=409,
                code='MEDIA_BUSY',
                title='Video is downloading',
                detail='The video is being downloaded right now. Retry in a minute.',
                request_id=request_id,
                retryable=True,
            )

        with transaction.atomic():
            media = Media.objects.select_for_update().select_related('source').get(pk=media.pk)
            # Every download that could still run: not started yet, a
            # failed one huey will retry (its retry reuses the row, with
            # scheduled_at moved on, which may already be due while it
            # waits for a worker), or one scheduled ahead. All are revoked
            # in huey and marked [revoked], so the media list reads
            # `skipped`, not `failed`, even for a failed manual download,
            # whose override=True would otherwise keep it current (see
            # mapping._task_has_override()) and download a skipped video.
            # A running one was refused above.
            queued = get_model_tasks(
                str(media.pk),
                name='download_media_file',
                qs=TaskHistory.objects.filter(
                    Q(start_at__isnull=True)
                    | Q(failed_at__isnull=False)
                    | Q(scheduled_at__gt=timezone.now()),
                ).exclude(
                    verbose_name__startswith=_REVOKED_VERBOSE_PREFIX,
                ),
            )
            for task in queued:
                _revoke_pending_index_task(task)
            if not (media.skip and media.manual_skip):
                media.skip = True
                media.manual_skip = True
                media.save()
        log.info(
            'medianest_bridge: media skip media_id=%s source_uuid=%s request_id=%s',
            media.pk, media.source_id, request_id,
        )
        return _media_response(media)
