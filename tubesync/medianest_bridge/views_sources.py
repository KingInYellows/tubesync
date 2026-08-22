'''
    T2 read endpoints: GET /sources (key-based lookup), GET /sources/{sourceUuid},
    GET /sources/{sourceUuid}/media (paginated).

    Deliberately does NOT implement /media/{mediaId}, /tasks, /tasks/{taskId},
    or a general paginated /sources list -- none exist in the vendored
    contract (bridge-openapi.v1.yaml). The M0 contract implements only the
    vertical-slice minimum by design ("only implement endpoints required by
    the vertical slice; keep the full roadmap documented without shipping
    unused half-implementations"); a desired-but-unspecified endpoint is a
    documented roadmap item, not something this app builds ahead of a
    contract update. If one is needed, the pattern is the same one that
    added REQUEST_TOO_LARGE to Error.code: propose the addition on the
    contract, get it accepted, re-vendor, then implement.

    All three views are read-only GET endpoints, so the T1 body-size gate's
    known limitation (BridgeView.dispatch() only inspects Content-Length,
    not actual bytes -- see errors.py and the T1 verifier's LOW finding)
    is not exercised by anything here; it remains a T3 obligation for
    whichever PR first adds a body-reading (write) endpoint.
'''
import uuid

from django.core.paginator import EmptyPage, Paginator
from django.db.models import F
from django.http import JsonResponse

from sync.models import Media, Source

from . import mapping
from .errors import error_response
from .views import BridgeView

DEFAULT_PAGE = 1
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _source_not_found(request_id):
    return error_response(
        status=404,
        code='SOURCE_NOT_FOUND',
        title='Source not found',
        detail='No source exists with the given identifier.',
        request_id=request_id,
        retryable=False,
    )


def _invalid_request(request_id, detail):
    # 400 SOURCE_INVALID is the closest honest contract code for a
    # malformed query parameter (missing "key", non-integer/out-of-range
    # page or limit) -- the contract has no dedicated
    # invalid-query-parameter code, and SOURCE_INVALID's own contract
    # description ("validation failure not tied to a namespace collision")
    # covers this without misdescribing it the way reusing an unrelated
    # code would.
    return error_response(
        status=400,
        code='SOURCE_INVALID',
        title='Invalid request',
        detail=detail,
        request_id=request_id,
        retryable=False,
    )


def _get_source_or_error(source_uuid, request_id):
    '''
        Returns (source, None) on success, or (None, error_response) on
        failure. A malformed UUID string and a well-formed-but-nonexistent
        UUID both return 404 SOURCE_NOT_FOUND, matching "invalid/unknown
        IDs -> 404 with the contract code" -- there is no separate 400 path
        for a syntactically bad ID.
    '''
    try:
        parsed = uuid.UUID(str(source_uuid))
    except (ValueError, AttributeError, TypeError):
        return None, _source_not_found(request_id)
    try:
        return Source.objects.get(pk=parsed), None
    except Source.DoesNotExist:
        return None, _source_not_found(request_id)


def _parse_pagination(request, request_id):
    '''
        Returns (page, limit, None) on success, or (None, None,
        error_response) on failure. Out-of-bounds or malformed values are
        rejected, not silently clamped: the contract's page/limit schema
        (minimum 1; limit maximum 200) declares the bounds of a *valid
        request*, not an instruction for the server to auto-correct
        whatever a caller sends.
    '''
    raw_page = request.GET.get('page', str(DEFAULT_PAGE))
    raw_limit = request.GET.get('limit', str(DEFAULT_LIMIT))
    try:
        page = int(raw_page)
        limit = int(raw_limit)
    except (TypeError, ValueError):
        return None, None, _invalid_request(request_id, 'page and limit must be integers.')
    if page < 1:
        return None, None, _invalid_request(request_id, 'page must be >= 1.')
    if limit < 1 or limit > MAX_LIMIT:
        return None, None, _invalid_request(
            request_id, f'limit must be between 1 and {MAX_LIMIT}.',
        )
    return page, limit, None


class SourceLookupView(BridgeView):
    '''GET /sources?key=<canonicalKey> -- listSourcesByKey (0 or 1 result).'''

    def get(self, request, *args, **kwargs):
        request_id = request._bridge_request_id
        key = request.GET.get('key', '').strip()
        if not key:
            return _invalid_request(request_id, 'The "key" query parameter is required.')
        source = Source.objects.filter(key=key).first()
        data = [mapping.serialize_source(source)] if source else []
        return JsonResponse({'data': data})


class SourceDetailView(BridgeView):
    '''GET /sources/{sourceUuid} -- getSource.'''

    def get(self, request, source_uuid, *args, **kwargs):
        request_id = request._bridge_request_id
        source, error = _get_source_or_error(source_uuid, request_id)
        if error:
            return error
        return JsonResponse(mapping.serialize_source(source))


class SourceMediaView(BridgeView):
    '''GET /sources/{sourceUuid}/media -- listSourceMedia, paginated.'''

    def get(self, request, source_uuid, *args, **kwargs):
        request_id = request._bridge_request_id
        source, error = _get_source_or_error(source_uuid, request_id)
        if error:
            return error
        page, limit, error = _parse_pagination(request, request_id)
        if error:
            return error
        # Ordering matches the existing upstream convention
        # (sync/views/sources.py SourceView.get_context_data) rather than
        # inventing a new one; .defer('metadata') skips the large raw
        # provider-JSON field the bridge response never uses.
        queryset = (
            Media.objects
            .filter(source=source)
            .select_related('source')
            .order_by(F('published').desc(nulls_last=True), 'pk')
            .defer('metadata')
        )
        paginator = Paginator(queryset, limit)
        try:
            page_obj = paginator.page(page)
        except EmptyPage:
            items = []
        else:
            media_rows = list(page_obj.object_list)
            download_tasks = mapping.batch_media_download_tasks(
                [media.pk for media in media_rows],
            )
            items = [
                mapping.serialize_media(
                    media,
                    download_task=download_tasks.get(str(media.pk), False),
                )
                for media in media_rows
            ]
        return JsonResponse({
            'data': items,
            'page': {'page': page, 'limit': limit, 'total': paginator.count},
        })
