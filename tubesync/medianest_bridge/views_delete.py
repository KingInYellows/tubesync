'''
    Contract 1.4.0 (MED-657, MediaNest ADR-0022):
    DELETE /sources/{sourceUuid}?deleteFiles=<bool> and
    DELETE /folders/{sourceKey}.

    Both are ordinary write routes: BridgeView.dispatch() applies the same
    gates as POST /sources (bearer token, CIDR, MEDIANEST_BRIDGE_READ_ONLY,
    body size) and writes the mutation audit line.

    Source deletion runs TubeSync's own delete (sync/source_delete.py), not
    a copy of it. The folder route only ever deletes a folder named
    `acq-src-<key>` under the configured download directories, and refuses
    while any source still uses that key or folder.
'''
from django.db import transaction
from django.http import JsonResponse

from common.logger import log
from sync.models import Source
from sync.source_delete import delete_source

from . import deletion
from .errors import error_response
from .views import BridgeView
from .views_sources import SourceDetailView, _invalid_request, _source_not_found
from .views_write import source_deleting

_TRUE = frozenset({'true', '1'})
_FALSE = frozenset({'false', '0', ''})


def _parse_delete_files(request):
    '''Returns (bool, None), or (None, detail) for an unusable value.'''
    raw = request.GET.get('deleteFiles')
    if raw is None:
        return False, None
    value = raw.strip().lower()
    if value in _TRUE:
        return True, None
    if value in _FALSE:
        return False, None
    return None, 'deleteFiles must be true or false.'


class SourceDetailDeleteView(SourceDetailView):
    '''
        GET /sources/{sourceUuid} (inherited) + DELETE /sources/{sourceUuid}.
        The contract's /sources/{sourceUuid} path item has both operations,
        so both live on one view class registered at one URL.
    '''

    def delete(self, request, source_uuid, *args, **kwargs):
        request_id = request._bridge_request_id
        delete_files, problem = _parse_delete_files(request)
        if problem:
            return _invalid_request(request_id, problem)
        parsed = deletion.parse_uuid(source_uuid)
        if parsed is None:
            return _source_not_found(request_id)

        with transaction.atomic():
            source = Source.objects.select_for_update().filter(pk=parsed).first()
            if source is not None and not deletion.is_pending_deletion(source):
                media_source = delete_source(source, delete_files=delete_files)
                log.info(
                    'medianest_bridge: source deleted source_uuid=%s delete_files=%s request_id=%s',
                    parsed, delete_files, request_id,
                )
                return JsonResponse(
                    deletion.serialize_deletion(parsed, media_source), status=202,
                )

        # Already deleted (by an earlier call, a concurrent one, or
        # TubeSync's UI) and still being cleaned up: same answer, no new
        # work. Gone completely, or never existed: 404.
        pending = deletion.pending_deletion_for_uuid(parsed)
        if pending is None:
            return _source_not_found(request_id)
        return JsonResponse(deletion.serialize_deletion(parsed, pending), status=202)


class FolderDeleteView(BridgeView):
    '''DELETE /folders/{sourceKey} -- deleteSourceFolder.'''

    def delete(self, request, source_key, *args, **kwargs):
        request_id = request._bridge_request_id
        if not deletion.valid_folder_key(source_key):
            return _invalid_request(
                request_id,
                'sourceKey must be 1-92 letters, digits, "_" or "-".',
            )

        holders = list(deletion.sources_using_folder(source_key))
        live = [s for s in holders if not deletion.is_pending_deletion(s)]
        if live:
            return error_response(
                status=409,
                code='SOURCE_CONFLICT',
                title='Folder still in use',
                detail='A source still uses this key or folder; delete the source first.',
                request_id=request_id,
                retryable=False,
                extra={'existingSourceUuid': str(live[0].pk)},
            )
        if holders:
            return source_deleting(request_id)

        try:
            folders = deletion.existing_folders(source_key)
        except deletion.FolderRefused as refused:
            log.warning(
                'medianest_bridge: refusing folder delete source_key=%s request_id=%s: %s',
                source_key, request_id, refused,
            )
            return _invalid_request(request_id, f'Refusing to delete this folder: {refused}.')
        if not folders:
            return error_response(
                status=404,
                code='FOLDER_NOT_FOUND',
                title='Folder not found',
                detail='No download folder exists for this key.',
                request_id=request_id,
                retryable=False,
            )

        removed = deletion.delete_folders(folders)
        log.info(
            'medianest_bridge: folder deleted source_key=%s folders=%s request_id=%s',
            source_key, ','.join(removed), request_id,
        )
        return JsonResponse({'canonicalKey': source_key, 'folders': removed})
