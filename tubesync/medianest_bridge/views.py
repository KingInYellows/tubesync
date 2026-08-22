'''
    T1 diagnostics + auth-skeleton views: GET /health/live, GET /health/ready,
    GET /meta, GET /capabilities. T2's source/media read endpoints live in
    views_sources.py; T3's write endpoints live in views_write.py -- all
    built on the same BridgeView here.
'''
import json
import time
import uuid
from datetime import datetime, timezone
from io import BytesIO

from django.conf import settings
from django.http import JsonResponse
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
    return supplied or str(uuid.uuid4())


def _read_body_is_oversized(request, limit):
    '''
        T3 hardening of the T1 body-size gate (T1 verifier LOW, carried
        forward through T2): actually reads the body, bounded to
        `limit + 1` bytes, rather than trusting the Content-Length
        header's stated value without verification -- and applies that
        same bound uniformly even when Content-Length is absent, instead
        of skipping the check entirely in that case (T1/T2's behavior).

        What this does and does NOT defend against, precisely: Django's
        own WSGIRequest wraps the WSGI input stream in a LimitedStream
        bounded by the Content-Length header's own value (defaulting to
        0 bytes if that header is absent or malformed --
        django.core.handlers.wsgi.WSGIRequest.__init__), and
        LimitedStream.read() enforces that bound even for a "read
        everything" call with no size argument. For this fork's
        synchronous gunicorn worker class (worker_class = 'sync' in
        gunicorn.py), that means a request can never actually deliver
        more bytes to this app than its own declared Content-Length,
        regardless of what this function does -- there is no "read
        unlimited bytes into memory" vulnerability at this layer to fix.
        What this function actually improves: (a) the size decision is
        based on bytes Django's stream actually handed back, not a
        client-supplied number taken on faith, and (b) a request with no
        Content-Length header no longer skips size checking outright.

        Populates request._body and request._stream (Django's own
        caching attributes) with the bytes read here, so a later
        request.body access downstream (used by the write views to
        parse JSON) returns this exact data rather than attempting to
        re-read the stream, which Django does not support once .read()
        has been called once.
    '''
    if getattr(request, '_body', None) is not None:
        return False
    try:
        chunk = request.read(limit + 1)
    except Exception:
        chunk = b''
    request._body = chunk
    request._stream = BytesIO(chunk)
    return len(chunk) > limit


def _response_json_field(response, field_name):
    '''
        Best-effort read of one field from a JSON response body, for
        logging only -- never raises, never used for control flow.
    '''
    try:
        return json.loads(response.content).get(field_name)
    except Exception:
        return None


def _log_outcome(request, response, request_id, duration_ms):
    '''
        T4: one structured line per request, for every route, success or
        error alike -- not a metrics stack (this fork has none, and
        upstream has no Prometheus hook to build on; researched, not
        assumed -- see the T4 PR body), just consistent, greppable
        request/outcome logging matching this app's existing
        common.logger.log usage. Exactly five fields, always the same
        five: route, method, status, duration, request id. Never the
        bearer token (nothing here ever touches it), never a filesystem
        path (route is the URL path, not a disk path).
    '''
    log.info(
        'medianest_bridge: request_complete route=%s method=%s status=%s duration_ms=%s request_id=%s',
        request.path, request.method, response.status_code, duration_ms, request_id,
    )


def _log_mutation_audit(request, response, request_id, kwargs):
    '''
        One audit-shaped line for every mutation *attempt* on a bridge
        route -- both accepted and rejected, including a read-only
        rejection, since "was a mutation attempted and what happened"
        is the audit-relevant fact regardless of outcome. Only called
        for non-safe methods (dispatch() checks this before calling).

        source_uuid is read from the URL kwargs when the route names one
        (POST /sources/{uuid}/sync) or, for a successful POST /sources
        (201, no uuid in the URL -- the uuid is newly assigned), from the
        response body's own uuid field. Never the bearer token, never a
        filesystem path.
    '''
    if 200 <= response.status_code < 300:
        outcome = 'accepted'
    else:
        code = _response_json_field(response, 'code') or str(response.status_code)
        outcome = f'rejected_{code}'
    source_uuid = kwargs.get('source_uuid') or _response_json_field(response, 'uuid')
    log.info(
        'medianest_bridge: mutation_audit route=%s outcome=%s source_uuid=%s request_id=%s',
        request.path, outcome, source_uuid or '-', request_id,
    )


class BridgeView(View):
    '''
        Shared gating for every bridge route, implemented as an overridden
        dispatch() rather than Django middleware. This is deliberate: adding
        an entry to settings.MIDDLEWARE would be a fourth upstream touch
        point, beyond the three the fork delta is scoped to (INSTALLED_APPS,
        the URL include, and the BASICAUTH_ALWAYS_ALLOW_URIS exemption).

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

        read_only_exempt (class attribute, default False): the read-only
        gate below blocks every non-GET/HEAD/OPTIONS request by HTTP
        method, which is the right default since every T1/T2 route is
        read-only and every other T3 write route (POST /sources,
        POST /sources/{uuid}/sync) genuinely mutates. POST
        /sources/validate is the one exception -- it never persists
        anything, and the vendored contract's own operation definition
        for it lists no 403 ReadOnly response at all (unlike /sources and
        /sources/{uuid}/sync, which both do). ValidateSourceView sets
        read_only_exempt = True to match the contract exactly, rather
        than blocking a genuinely non-mutating request because of its
        HTTP method alone.
    '''

    read_only_exempt = False

    def dispatch(self, request, *args, **kwargs):
        '''
            Thin wrapper around _run_gates(): computes request_id/
            correlation_id once, then -- regardless of which internal
            gate (or the concrete view itself) produced the response --
            logs exactly one structured outcome line and, for a mutation
            attempt, exactly one audit line. This is why the gate logic
            below lives in a separate method rather than dispatch()
            itself: _run_gates() has several early `return
            error_response(...)` points (disabled, CIDR, auth, read-only,
            body-size), and duplicating the outcome/audit logging at each
            of those return points would be easy to miss one on a future
            edit. One wrapper, one place, applies to all of them
            uniformly.
        '''
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
        start = time.monotonic()
        response = self._run_gates(request, request_id, *args, **kwargs)
        duration_ms = round((time.monotonic() - start) * 1000, 2)
        _log_outcome(request, response, request_id, duration_ms)
        if request.method not in SAFE_METHODS:
            _log_mutation_audit(request, response, request_id, kwargs)
        return response

    def _run_gates(self, request, request_id, *args, **kwargs):
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
            if (
                request.method not in SAFE_METHODS
                and config.is_read_only()
                and not self.read_only_exempt
            ):
                return error_response(
                    status=403,
                    code='PROVIDER_READ_ONLY',
                    title='Bridge is read-only',
                    detail='MEDIANEST_BRIDGE_READ_ONLY is enabled; mutating requests are rejected.',
                    request_id=request_id,
                    retryable=False,
                )
            if _read_body_is_oversized(request, config.max_body_bytes()):
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
            # T2: read endpoints. T3: validate/create/sync-now (both
            # channel and playlist go through the same SourceForm-based
            # validate/create logic, so both flags flip together, not
            # independently). updateSource/disableSource/deleteSource
            # stay false -- no such endpoints exist anywhere in this app.
            'readSources': True,
            'validateChannelSource': True,
            'validatePlaylistSource': True,
            'createChannelSource': True,
            'createPlaylistSource': True,
            'updateSource': False,
            'disableSource': False,
            'deleteSource': False,
            'syncSource': True,
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
