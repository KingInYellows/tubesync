'''
    Contract 1.5.0 (MED-730, MediaNest ADR-0030): video lists.

    POST /videolists                               createVideoList
    POST /sources/{sourceUuid}/videos              addVideo
    DELETE /sources/{sourceUuid}/videos/{videoId}  removeVideo

    All three are ordinary write routes behind BridgeView.dispatch()'s
    gates (token, CIDR, MEDIANEST_BRIDGE_READ_ONLY, body size, audit line).
    What a video list is, and why it needs no migration, is in
    videolist.py's docstring.
'''
from django.http import HttpResponse, JsonResponse

from . import deletion, mapping, videolist
from .errors import error_response
from .request_schemas import validate_add_video_request, validate_create_videolist_request
from .source_forms import allowed_source_default_fields, contract_source_type_to_tubesync
from .views import BridgeView
from .views_delete import _parse_delete_files
from .views_sources import _get_source_or_error, _invalid_request
from .views_write import (
    _invalid,
    _parse_json_body,
    _source_defaults_or_error,
    save_new_source,
    source_deleting,
)

_PLAYLIST = 'playlist'


def videolist_overlay(playlist_overlay):
    '''
        The playlist overlay from MEDIANEST_BRIDGE_SOURCE_DEFAULTS (media
        format, NFO, thumbnails, quality) with videolist.FORCED_FIELDS on
        top. Only SourceForm fields are applied.
    '''
    allowed = allowed_source_default_fields()
    overlay = dict(playlist_overlay or {})
    overlay.update({
        field: value for field, value in videolist.FORCED_FIELDS.items()
        if field in allowed
    })
    return overlay


class CreateVideoListView(BridgeView):
    '''
        POST /videolists -- create-or-adopt a video list. Same responses as
        POST /sources: 201 with the Source, 409 SOURCE_CONFLICT with
        existingSourceUuid when the key exists (adopt it), 409
        SOURCE_DELETING while a deleted source with this key is still being
        removed, 400 SOURCE_NAMESPACE_CONFLICT for a name or directory
        another source uses.
    '''

    def post(self, request, *args, **kwargs):
        request_id = request._bridge_request_id
        body, error = _parse_json_body(request, request_id)
        if error:
            return error
        schema_errors = validate_create_videolist_request(body)
        if schema_errors:
            return _invalid(request_id, schema_errors)

        defaults_by_type, defaults_error = _source_defaults_or_error(
            request_id, route='POST /videolists', source_type=_PLAYLIST,
        )
        if defaults_error:
            return defaults_error

        return save_new_source(
            request_id,
            tubesync_source_type=contract_source_type_to_tubesync(_PLAYLIST),
            canonical_key=body['canonicalKey'],
            name=body['name'],
            directory=body['directory'],
            defaults_overlay=videolist_overlay(defaults_by_type[_PLAYLIST]),
        )


def _videolist_or_error(source_uuid, request_id):
    source, error = _get_source_or_error(source_uuid, request_id)
    if error:
        return None, error
    if deletion.is_pending_deletion(source):
        return None, source_deleting(request_id)
    if not videolist.is_videolist(source):
        return None, _invalid_request(
            request_id, 'This source is not a video list; only video lists take single videos.',
        )
    return source, None


class SourceVideosView(BridgeView):
    '''
        POST /sources/{sourceUuid}/videos -- addVideo. 201 with the new
        MediaItem, or 200 with the existing one when the video is already
        on this list (idempotent).
    '''

    def post(self, request, source_uuid, *args, **kwargs):
        request_id = request._bridge_request_id
        body, error = _parse_json_body(request, request_id)
        if error:
            return error
        schema_errors = validate_add_video_request(body)
        if schema_errors:
            return _invalid(request_id, schema_errors)
        source, error = _videolist_or_error(source_uuid, request_id)
        if error:
            return error
        media, created = videolist.add_video(source, body['videoId'])
        return JsonResponse(mapping.serialize_media(media), status=201 if created else 200)


class SourceVideoDetailView(BridgeView):
    '''
        DELETE /sources/{sourceUuid}/videos/{videoId}?deleteFiles=<bool> --
        removeVideo. 204 whether or not the video was on the list, so a
        retried call is safe. Files are kept unless deleteFiles=true. 409
        MEDIA_BUSY (retryable) while the video is downloading.
    '''

    def delete(self, request, source_uuid, video_id, *args, **kwargs):
        request_id = request._bridge_request_id
        delete_files, problem = _parse_delete_files(request)
        if problem:
            return _invalid_request(request_id, problem)
        if not videolist.VIDEO_ID_PATTERN.match(video_id or ''):
            return _invalid_request(request_id, 'videoId must be an 11-character YouTube video ID.')
        source, error = _videolist_or_error(source_uuid, request_id)
        if error:
            return error
        try:
            videolist.remove_video(source, video_id, delete_files=delete_files)
        except videolist.VideoBusy:
            return error_response(
                status=409,
                code='MEDIA_BUSY',
                title='Video is downloading',
                detail='The video is being downloaded right now. Retry in a minute.',
                request_id=request_id,
                retryable=True,
            )
        return HttpResponse(status=204)
