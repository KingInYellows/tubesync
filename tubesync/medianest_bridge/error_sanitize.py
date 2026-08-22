'''
    Sanitizes a TubeSync-sourced error string before it crosses the bridge
    boundary. Two call sites, both load-bearing:

    - mapping.py::serialize_media() -- MediaItem.error. T4's original
      motivation (T3 verifier INFO note): sync.tasks.get_error_message()
      strips only the exception-type prefix off TaskHistory.last_error's
      last line -- a filesystem path, a cookie-file reference, or
      anything secret-shaped in the remainder reached MediaItem.error
      unredacted otherwise.
    - errors.py::error_response() -- every RFC 7807 envelope's `detail`
      field, universally. Added after a T4 verifier MEDIUM: POST
      /sources' directory-traversal validation error was building its
      detail from `str(form.errors)` (Django's own HTML-rendering
      __str__, plus an earlier version of the traversal message that
      embedded DOWNLOAD_ROOT verbatim) -- neither symptom was actually
      about mapping.py, they were about every OTHER error-emitting call
      site not going through this module at all. Wiring the sanitizer
      into error_response() itself, not each individual call site, is
      what makes "every current and future error emitter inherits this"
      literally true rather than an aspiration -- see views_write.py's
      _clean_form_errors() for the accompanying fix (Django form errors
      must never be str()'d into a response either, independent of
      sanitization: that produces `<ul class="errorlist">` HTML).

    Scope, deliberately bounded rather than a general secret-scanner:
    1. settings.DOWNLOAD_ROOT-prefixed and settings.COOKIES_FILE-prefixed
       paths specifically (the two filesystem locations this app's own
       config actually names) -- checked first, most specific.
    2. Any other absolute Unix-style path shape (a defensible general
       pattern; this fork only ever runs in Linux containers per its
       Dockerfile, so a Windows-path pattern would be dead code, not
       added).
    3. key=value / key: value pairs whose key looks credential-shaped
       (token, password, secret, auth, cookie -- matched as a WHOLE
       underscore-delimited segment of the identifier, e.g. "auth_token"
       matches on its "auth" segment, "auth_token" also on its "token"
       segment) -- redacts only the *value*, not the whole message, and
       only when a whole segment of the key name signals a credential.

       T4 verifier LOW, fixed here: an earlier substring-based version of
       this pattern over-redacted "author=" (contains "auth" as a
       substring, not a segment) and "turkey:" (contains "key" as a
       substring). Segment-exact matching fixes both without narrowing
       what it's supposed to catch -- "auth_token"/"api_secret" still
       match on their own segments.

       "key" is deliberately NOT in the credential-word list at all, not
       even as an exact segment: TubeSync's own domain uses "key" as a
       core NON-secret field name (Source.key, Media.key -- the external
       channel/playlist/video identifier), and a SQL error message is
       very plausibly going to say something like "UNIQUE constraint
       failed: sync_source.key" or mention "primary_key" -- redacting
       those would destroy exactly the diagnostic detail this app is
       trying to preserve, for a word that carries no credential
       signal in this app's own domain. (Contrast with "auth"/"token"/
       "secret"/"cookie"/"password", none of which TubeSync uses as a
       legitimate field name.)

    Deliberately does NOT blanket-redact any long alphanumeric-looking
    token: a YouTube channel ID ('UCxxxxxxxxxxxxxxxxxxxxxx', 24 chars) or
    video ID (11 chars) appearing in an error message is exactly the kind
    of diagnostic detail MediaItem.error should keep -- over-redacting
    would trade a real, bounded leak risk for a real, guaranteed loss of
    debugging value on every single error, which is not a good trade.

    The error CLASS/summary text itself (e.g. "HTTP Error 403: Forbidden",
    "video unavailable") is never touched -- only the three patterns
    above.
'''
import re

_REDACTED = '<redacted>'

# Matches a Unix absolute path with at least one path component after the
# leading slash. The lookbehind rejects "://" URL schemes (second slash
# is preceded by "/") and host-relative URL paths such as ".com/watch"
# (the slash is preceded by a hostname character).
_GENERIC_PATH_PATTERN = re.compile(r'(?<![:/\w])/[^\s\'"]+')

# Deliberately excludes "key" -- see this module's docstring, point 3.
# The credential word must be the WHOLE identifier or a WHOLE
# underscore-delimited segment of it (matches "auth_token", "api_secret";
# does not match "author", "turkey", "cookies" (plural, a different word,
# not a segment) -- found via a real over-redaction bug during T4 review:
# an earlier version matched ANY \w+ before ':'/'=' and checked
# credential-ness afterward, which let a plain English "rejected: " colon
# earlier in the same message get greedily matched as the key=value pair
# instead, swallowing the REAL "auth_token=..." into its "value" and
# leaving it unredacted entirely -- the fix anchors the credential word
# directly into what the regex is allowed to match in the first place,
# not decided in a separate Python check after an over-broad match.
_IDENTIFIER_KEY_VALUE_PATTERN = re.compile(
    r'(?i)\b'
    r'((?:token|password|passwd|secret|auth|cookie)(?:_\w+)*'
    r'|\w+_(?:token|password|passwd|secret|auth|cookie)(?:_\w*)*)'
    r'(\s*[:=]\s*)(\S+)',
)


def _redact_credential_kv(match):
    identifier, separator = match.group(1), match.group(2)
    return f'{identifier}{separator}{_REDACTED}'


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
    sanitized = _IDENTIFIER_KEY_VALUE_PATTERN.sub(_redact_credential_kv, sanitized)
    return sanitized
