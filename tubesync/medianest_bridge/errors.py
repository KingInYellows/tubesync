'''
    RFC 7807-style error envelope, adopted verbatim from
    bridge-openapi.v1.yaml's components.schemas.Error.

    Every field in ERROR_CODES corresponds to a code in the contract's
    `code` enum.
'''
from django.http import JsonResponse


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


def error_response(*, status, code, title, detail, request_id, retryable):
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
        'detail': detail,
        'requestId': request_id,
        'retryable': retryable,
    }
    response = JsonResponse(body, status=status)
    response['X-Request-ID'] = request_id
    return response
