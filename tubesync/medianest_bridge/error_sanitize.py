'''
    Sanitizes a TubeSync-sourced error string before it crosses the bridge
    boundary into MediaItem.error. T4 due obligation, carried forward from
    the T3 verifier's INFO note: sync.tasks.get_error_message() (reused by
    mapping.py::serialize_media for MediaItem.error) strips only the
    exception-type prefix off TaskHistory.last_error's last line -- a
    filesystem path, a cookie-file reference, or anything secret-shaped
    embedded in the remainder of that line reaches MediaItem.error, and
    now crosses to MediaNest, unredacted. This module is that redaction
    layer, applied once here so every current and future serializer that
    emits an error string inherits it (mapping.py calls this, not the
    other way around).

    Scope, deliberately bounded rather than a general secret-scanner:
    1. settings.DOWNLOAD_ROOT-prefixed and settings.COOKIES_FILE-prefixed
       paths specifically (the two filesystem locations this app's own
       config actually names) -- checked first, most specific.
    2. Any other absolute Unix-style path shape (a defensible general
       pattern; this fork only ever runs in Linux containers per its
       Dockerfile, so a Windows-path pattern would be dead code, not
       added).
    3. key=value / key: value pairs whose key looks credential-shaped
       (token, password, secret, api key, auth, cookie) -- redacts only
       the *value*, not the whole message, and only when the key name
       itself signals a credential. Deliberately does NOT blanket-redact
       any long alphanumeric-looking token: a YouTube channel ID
       ('UCxxxxxxxxxxxxxxxxxxxxxx', 24 chars) or video ID (11 chars)
       appearing in an error message is exactly the kind of diagnostic
       detail MediaItem.error should keep -- over-redacting would trade a
       real, bounded leak risk for a real, guaranteed loss of debugging
       value on every single error, which is not a good trade.

    The error CLASS/summary text itself (e.g. "HTTP Error 403: Forbidden",
    "video unavailable") is never touched -- only the three patterns
    above.
'''
import re

_REDACTED = '<redacted>'

# Matches a run of non-whitespace, non-quote characters starting with '/'
# and containing at least one more '/' -- a generic Unix absolute-path
# shape. Deliberately requires a second slash so a lone leading '/' in
# prose (rare, but possible) doesn't get redacted as if it were a path.
_GENERIC_PATH_PATTERN = re.compile(r'/[^\s\'"]*/[^\s\'"]*')

_CREDENTIAL_KEY_VALUE_PATTERN = re.compile(
    # Matches a whole snake_case/compound identifier that *contains* one of
    # these credential-flavored substrings (e.g. "auth_token", "api_key",
    # "COOKIE_SECRET"), immediately followed by ':' or '=' and a value.
    # \w includes '_', so a bare \b(token|...)\b would never match
    # "auth_token" -- the boundary between 'h' and '_' isn't a word
    # boundary at all. \w* on both sides absorbs the compound identifier
    # instead of requiring an exact bare keyword.
    r'(?i)\b(\w*(?:token|password|passwd|secret|key|auth|cookie)\w*)\s*[:=]\s*(\S+)',
)


def _configured_paths():
    from django.conf import settings
    paths = []
    for attr in ('DOWNLOAD_ROOT', 'COOKIES_FILE'):
        value = getattr(settings, attr, None)
        if value:
            paths.append(str(value))
    return paths


def sanitize_error_message(message):
    if not message:
        return message
    sanitized = message
    for configured_path in _configured_paths():
        if not configured_path:
            continue
        # Redact the configured path and anything path-shaped
        # immediately following it (e.g. a filename under DOWNLOAD_ROOT),
        # before the generic pattern below would otherwise leave the
        # configured-path prefix and the rest as two separate matches.
        pattern = re.compile(re.escape(configured_path) + r'[^\s\'"]*')
        sanitized = pattern.sub(_REDACTED, sanitized)
    sanitized = _GENERIC_PATH_PATTERN.sub(_REDACTED, sanitized)
    sanitized = _CREDENTIAL_KEY_VALUE_PATTERN.sub(
        lambda m: f'{m.group(1)}={_REDACTED}', sanitized,
    )
    return sanitized
