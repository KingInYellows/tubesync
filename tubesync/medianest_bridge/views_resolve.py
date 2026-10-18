'''
    MED-589 (contract 1.1.0): POST /sources/resolve -- resolves a
    `/@handle`, `/c/NAME` or `/user/NAME` channel URL to the channel's
    stable `UC...` ID with one yt-dlp metadata lookup. See
    channel_resolve.py for the URL shape check and the lookup itself.

    Persists nothing, so read_only_exempt = True (the contract's own
    operation lists no 403 ReadOnly response, the same as
    POST /sources/validate). Disabled/CIDR/bearer/body-size gating comes
    from BridgeView.dispatch() like every other route.
'''
from django.http import JsonResponse

from common.logger import log

from .channel_resolve import (
    CHANNEL_ID_PATTERN,
    ChannelLookupError,
    lookup_channel_identity,
    parse_channel_handle_url,
)
from .error_sanitize import sanitize_error_message
from .errors import error_response
from .request_schemas import validate_resolve_source_request
from .views import BridgeView
from .views_write import _invalid, _parse_json_body

# Server-side log only; yt-dlp's message is sanitized and bounded before
# it is logged, and never reaches a response body.
_LOG_REASON_MAX_LENGTH = 500


def _log_lookup_failure(request_id, outcome, lookup_url, reason):
    log.warning(
        'medianest_bridge: resolve_lookup outcome=%s lookup_url=%s request_id=%s reason=%s',
        outcome, lookup_url, request_id,
        sanitize_error_message(reason or '')[:_LOG_REASON_MAX_LENGTH],
    )


class ResolveSourceView(BridgeView):
    '''
        POST /sources/resolve. Order: body parse -> request schema ->
        URL shape (400 SOURCE_INVALID for all three) -> lookup of the
        bridge's own rebuilt URL -> 404 SOURCE_NOT_FOUND when YouTube
        reports no such channel, 503 PROVIDER_UNAVAILABLE (retryable)
        for any other lookup failure, 400 SOURCE_INVALID when the lookup
        returns no valid channel ID.

        Expected lookup outcomes are logged at warning, never error: a
        missing channel or a transient YouTube failure is not a bridge
        fault.
    '''

    read_only_exempt = True

    def post(self, request, *args, **kwargs):
        request_id = request._bridge_request_id
        body, error = _parse_json_body(request, request_id)
        if error:
            return error
        schema_errors = validate_resolve_source_request(body)
        if schema_errors:
            return _invalid(request_id, schema_errors)

        lookup_url, shape_error = parse_channel_handle_url(body['url'])
        if shape_error:
            return _invalid(request_id, [shape_error])

        try:
            channel_id, display_name = lookup_channel_identity(lookup_url)
        except ChannelLookupError as e:
            if e.not_found:
                _log_lookup_failure(request_id, 'not_found', lookup_url, e.reason)
                return error_response(
                    status=404,
                    code='SOURCE_NOT_FOUND',
                    title='Channel not found',
                    detail='YouTube reports no channel for this URL.',
                    request_id=request_id,
                    retryable=False,
                )
            _log_lookup_failure(request_id, 'provider_unavailable', lookup_url, e.reason)
            return error_response(
                status=503,
                code='PROVIDER_UNAVAILABLE',
                title='Channel lookup failed',
                detail='The channel lookup could not be completed; retry later.',
                request_id=request_id,
                retryable=True,
            )

        if not channel_id or not CHANNEL_ID_PATTERN.fullmatch(channel_id):
            _log_lookup_failure(request_id, 'invalid_channel_id', lookup_url, None)
            return _invalid(request_id, ['lookup returned no valid channel id.'])

        return JsonResponse({
            'sourceType': 'channel',
            'canonicalKey': channel_id,
            'canonicalUrl': f'https://www.youtube.com/channel/{channel_id}',
            'displayName': display_name,
        })
