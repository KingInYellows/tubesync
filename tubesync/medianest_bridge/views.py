'''
    T1 diagnostics + auth-skeleton views: GET /health/live, GET /health/ready,
    GET /meta, GET /capabilities. T2's source/media read endpoints live in
    views_sources.py, built on the same BridgeView here. No write endpoints
    yet -- those are T3.
'''
import uuid
from datetime import datetime, timezone

from django.conf import settings
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import View

from common.logger import log

from . import config, readiness
from .auth import bearer_token_valid, cidr_allowed, cidr_trust_warning
from .errors import error_response

SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})


def _now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def _resolve_request_id(request):
    supplied = request.META.get('HTTP_X_REQUEST_ID', '').strip()
    if supplied:
        try:
            return str(uuid.UUID(supplied))
        except ValueError:
            pass
    return str(uuid.uuid4())


@method_decorator(csrf_exempt, name='dispatch')
class BridgeView(View):
    '''
        Shared gating for every bridge route, implemented as an overridden
        dispatch() rather than Django middleware. This is deliberate: adding
        an entry to settings.MIDDLEWARE would be a fourth upstream touch
        point, beyond the three the fork delta is scoped to (INSTALLED_APPS,
        the URL include, and the BASICAUTH_PREFIX_ALLOW_URIS exemption).

        CSRF is exempted here (not via settings) for the same reason: the
        bridge is a bearer-token server-to-server API; MediaNest callers
        never carry Django's CSRF cookie/token. Without this exemption,
        CsrfViewMiddleware would reject mutating requests with an HTML 403
        before dispatch() runs.

        Gate order (checked in this sequence, each short-circuiting on
        failure): disabled -> CIDR -> bearer -> read-only -> body-size ->
        the concrete view's handler. Disabled is checked first because an
        unconfigured bridge should fail closed identically regardless of
        what a caller sends; CIDR before bearer so a network-level reject
        never even looks at (or logs) a credential.

        Immediately before the CIDR check itself, cidr_trust_warning()
        (auth.py) is evaluated on every request while the bridge is
        enabled: when MEDIANEST_BRIDGE_ALLOWED_CIDRS is configured but
        LISTEN_HOST is not loopback, the CIDR gate's trust in X-Real-IP no
        longer holds (see auth.py's module docstring), and this logs a
        loud warning rather than silently degrading. It never blocks the
        request -- an operator may have a legitimate alternate ingress
        this app cannot verify -- and never mentions the bearer token.
    '''

    def dispatch(self, request, *args, **kwargs):
        request_id = _resolve_request_id(request)
        # Stashed so a concrete view's own error responses (e.g. 404
        # SOURCE_NOT_FOUND, 400 SOURCE_INVALID in views_sources.py) can
        # reuse the exact same request_id dispatch() will echo in the
        # X-Request-ID response header below, rather than resolving a
        # second, different one.
        request._bridge_request_id = request_id
        correlation_id = request.META.get('HTTP_X_CORRELATION_ID', '').strip()
        log.debug(
            'medianest_bridge: request path=%s method=%s request_id=%s correlation_id=%s',
            request.path, request.method, request_id, correlation_id or '-',
        )
        try:
            if not config.bridge_enabled():
                return error_response(
                    status=503,
                    code='PROVIDER_UNAVAILABLE',
                    title='Bridge disabled',
                    detail='MEDIANEST_BRIDGE_TOKEN_FILE is not configured or unreadable; the bridge is disabled fail-closed.',
                    request_id=request_id,
                    retryable=True,
                )
            warning = cidr_trust_warning()
            if warning:
                log.warning(warning)
            if not cidr_allowed(request):
                # Per bridge-openapi.v1.yaml's Unauthorized response
                # ("Missing or invalid bearer token, or source CIDR not
                # allowlisted"), a CIDR mismatch uses the same 401 as an
                # auth failure, not 403 -- the contract defines no 403 for
                # this case (its only 403 is ReadOnly).
                return error_response(
                    status=401,
                    code='PROVIDER_AUTHENTICATION_FAILED',
                    title='Client not allowlisted',
                    detail='Client address is not within MEDIANEST_BRIDGE_ALLOWED_CIDRS.',
                    request_id=request_id,
                    retryable=False,
                )
            if not bearer_token_valid(request):
                return error_response(
                    status=401,
                    code='PROVIDER_AUTHENTICATION_FAILED',
                    title='Missing or invalid bearer token',
                    detail='Authorization: Bearer <token> is required.',
                    request_id=request_id,
                    retryable=False,
                )
            if request.method not in SAFE_METHODS and config.is_read_only():
                return error_response(
                    status=403,
                    code='PROVIDER_READ_ONLY',
                    title='Bridge is read-only',
                    detail='MEDIANEST_BRIDGE_READ_ONLY is enabled; mutating requests are rejected.',
                    request_id=request_id,
                    retryable=False,
                )
            if request.method not in SAFE_METHODS:
                content_length = request.META.get('CONTENT_LENGTH')
                length = None
                if content_length not in (None, ''):
                    try:
                        length = int(content_length)
                    except (TypeError, ValueError):
                        length = None
                if length is None:
                    transfer_encoding = (
                        request.META.get('HTTP_TRANSFER_ENCODING') or ''
                    ).lower()
                    if 'chunked' in transfer_encoding:
                        return error_response(
                            status=413,
                            code='REQUEST_TOO_LARGE',
                            title='Request body too large',
                            detail=(
                                'Chunked request bodies cannot be size-checked '
                                'before read; use Content-Length instead.'
                            ),
                            request_id=request_id,
                            retryable=False,
                        )
                elif length > config.max_body_bytes():
                    return error_response(
                        status=413,
                        code='REQUEST_TOO_LARGE',
                        title='Request body too large',
                        detail=f'Request body exceeds MEDIANEST_BRIDGE_MAX_BODY_BYTES ({config.max_body_bytes()} bytes).',
                        request_id=request_id,
                        retryable=False,
                    )
            response = super().dispatch(request, *args, **kwargs)
        except Exception:
            # No raw stack trace, no exception message, ever reaches the
            # caller. Full detail goes to the server-side log only.
            log.exception(
                'medianest_bridge: unhandled exception request_id=%s path=%s',
                request_id, request.path,
            )
            return error_response(
                status=500,
                code='INTERNAL_PROVIDER_ERROR',
                title='Internal bridge error',
                detail='An unexpected error occurred while handling this request.',
                request_id=request_id,
                retryable=True,
            )
        response['X-Request-ID'] = request_id
        return response


class HealthLiveView(BridgeView):

    def get(self, request, *args, **kwargs):
        return JsonResponse({'status': 'alive', 'checkedAt': _now_iso()})


class HealthReadyView(BridgeView):

    def get(self, request, *args, **kwargs):
        components = readiness.collect_components()
        body = {
            'status': readiness.aggregate_status(components),
            'checkedAt': _now_iso(),
            'components': components,
        }
        return JsonResponse(body)


class MetaView(BridgeView):

    def get(self, request, *args, **kwargs):
        body = {
            'bridgeVersion': config.BRIDGE_VERSION,
            # settings.VERSION is known-stale vs the checked-out commit
            # (upstream audit finding); upstreamCommit is the field callers
            # should pin against.
            'tubesyncVersion': getattr(settings, 'VERSION', 'unknown'),
            'upstreamCommit': config.upstream_sha(),
        }
        return JsonResponse(body)


class CapabilitiesView(BridgeView):

    def get(self, request, *args, **kwargs):
        body = {
            'health': True,
            # T2: GET /sources, GET /sources/{uuid}, GET /sources/{uuid}/media
            # are implemented (read-only). Everything below stays false
            # until the corresponding write/task endpoints ship in T3+.
            'readSources': True,
            'validateChannelSource': False,
            'validatePlaylistSource': False,
            'createChannelSource': False,
            'createPlaylistSource': False,
            'updateSource': False,
            'disableSource': False,
            'deleteSource': False,
            'syncSource': False,
            'readMedia': True,
            'retryMedia': False,
            'skipMedia': False,
            'enableMedia': False,
            'readTasks': False,
            'cancelTask': False,
            'retryTask': False,
            'oneOffVideoDownload': False,
            'pushEvents': False,
            'plexScan': False,
            'plexCollectionCreation': False,
        }
        return JsonResponse(body)
