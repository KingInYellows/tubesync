'''
    RFC 7807-style error envelope, adopted verbatim from
    bridge-openapi.v1.yaml's components.schemas.Error.

    Every code in _SLUGS corresponds to a code in the vendored contract's
    `code` enum. REQUEST_TOO_LARGE was flagged as a contract gap during T1
    (the slice-1 enum had no honest code for an oversized-request-body
    rejection) and the contract owner accepted it as a canonical addition
    for the T2 re-vendor (bridge-openapi.v1.yaml @
    713f9b4ac9efc24e0f285f9af58a50276f29ebb9) -- it is no longer a proposed
    addition, just a normal contract code, kept in this dict like the rest.

    T4 verifier MEDIUM fix: `detail` is run through
    error_sanitize.sanitize_error_message() unconditionally, here at the
    envelope construction point -- not left as something every individual
    call site has to remember to do. This is the wiring point the T4
    redaction work was originally supposed to land at; mapping.py-only
    wiring left every OTHER error emitter (every `error_response()` call
    across views.py/views_write.py/views_sources.py) unsanitized, which
    is exactly how POST /sources' directory-traversal error leaked
    DOWNLOAD_ROOT verbatim.
'''
from django.http import JsonResponse

from .error_sanitize import sanitize_error_message


# type URIs are illustrative identifiers, not fetchable documents, matching
# the contract's own examples (e.g. "https://medianest.local/problems/source-conflict").
_TYPE_BASE = 'https://medianest.local/problems/'

_SLUGS = {
    'PROVIDER_UNAVAILABLE': 'provider-unavailable',
    'PROVIDER_AUTHENTICATION_FAILED': 'provider-authentication-failed',
    'PROVIDER_READ_ONLY': 'provider-read-only',
    'SOURCE_INVALID': 'source-invalid',
    'SOURCE_CONFLICT': 'source-conflict',
    'SOURCE_NAMESPACE_CONFLICT': 'source-namespace-conflict',
    'SOURCE_NOT_FOUND': 'source-not-found',
    'MEDIA_NOT_FOUND': 'media-not-found',
    'RATE_LIMITED': 'rate-limited',
    'INTERNAL_PROVIDER_ERROR': 'internal-provider-error',
    'REQUEST_TOO_LARGE': 'request-too-large',
}


def error_response(*, status, code, title, detail, request_id, retryable, extra=None):
    if code not in _SLUGS:
        # Defensive: every call site in this app must use a known code.
        # Coding this as a hard failure (not a silent fallback) makes a
        # typo'd code fail loudly in bridge tests rather than shipping a
        # response that doesn't validate against the contract.
        raise ValueError(f'Unknown bridge error code: {code!r}')
    body = {
        'type': _TYPE_BASE + _SLUGS[code],
        'title': title,
        'status': status,
        'code': code,
        'detail': sanitize_error_message(detail),
        'requestId': request_id,
        'retryable': retryable,
    }
    if extra:
        # Contract extension point: SOURCE_CONFLICT's 409 response is
        # `allOf: [Error, {existingSourceUuid: ...}]` -- a plain envelope
        # field plus one extra key, not a different shape. `extra` lets a
        # caller add that single key without hand-building a second
        # JsonResponse around a round-tripped copy of this one.
        body.update(extra)
    response = JsonResponse(body, status=status)
    response['X-Request-ID'] = request_id
    return response
