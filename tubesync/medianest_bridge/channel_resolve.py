'''
    MED-589: resolve a YouTube channel handle URL (`/@handle`, `/c/NAME`,
    `/user/NAME`) to its stable `UC...` channel ID, for
    POST /sources/resolve (views_resolve.py).

    Two halves, kept separate so the URL check is a pure function the
    tests can exercise without any yt-dlp involvement:

    - parse_channel_handle_url(): strict shape check of the caller's URL.
      Returns the lookup URL the bridge itself builds from the parsed
      parts -- the caller's URL is never handed to yt-dlp verbatim, so
      nothing it carries beyond the validated handle/name (other hosts,
      ports, credentials, extra path segments, a query string) can ever
      reach YouTube egress.
    - lookup_channel_identity(): one flat yt-dlp metadata lookup of that
      rebuilt URL, configured the way sync/youtube.py's get_channel_id()
      does it (get_yt_opts(), so cookies apply; extract_flat,
      playlist_items '1', no download/format checks), plus a socket
      timeout and a single extractor retry so a dead network cannot hold
      a synchronous gunicorn worker (timeout 90s) for long. Lives here
      rather than as a new sync/youtube.py helper so the fork's upstream
      touch points (README "Fork delta") stay unchanged; it only imports
      get_yt_opts() from there.

    The lookup runs inside TubeSync, so it uses TubeSync's own YouTube
    egress (the VPN boundary, ADR-0006 Sec.2); MediaNest never contacts
    YouTube itself.
'''
import re
from urllib.parse import urlsplit

import yt_dlp

from common.logger import log

from .error_sanitize import sanitize_error_message

ALLOWED_HOSTS = frozenset({'youtube.com', 'www.youtube.com', 'm.youtube.com'})
ALLOWED_TABS = ('videos', 'shorts', 'streams', 'featured', 'playlists', 'about')

# YouTube handles: 3-30 of letters, digits, '_', '-', '.'. Legacy /c/
# and /user/ names are looser on YouTube's side; 1-100 of the same
# charset is a safe superset of what this bridge needs to accept. Any
# percent-encoded or non-ASCII character fails these on purpose.
_HANDLE_RE = r'[A-Za-z0-9_.-]{3,30}'
_NAME_RE = r'[A-Za-z0-9_.-]{1,100}'
_TABS_RE = '|'.join(ALLOWED_TABS)
_PATH_PATTERN = re.compile(
    r'^/(?:'
    rf'@(?P<handle>{_HANDLE_RE})'
    rf'|c/(?P<custom>{_NAME_RE})'
    rf'|user/(?P<user>{_NAME_RE})'
    rf')(?:/(?:{_TABS_RE}))?/?$'
)

CHANNEL_ID_PATTERN = re.compile(r'^UC[A-Za-z0-9_-]{22}$')

LOOKUP_SOCKET_TIMEOUT_SECONDS = 10
LOOKUP_EXTRACTOR_RETRIES = 1
DISPLAY_NAME_MAX_LENGTH = 200

_SHAPE_ERROR = (
    'url must name a YouTube channel by handle (@handle), custom URL '
    '(c/NAME) or legacy username (user/NAME), optionally followed by a '
    f'trailing slash or one of the tabs {list(ALLOWED_TABS)!r}.'
)


class ChannelLookupError(Exception):
    '''
        The yt-dlp lookup failed. `not_found` is True when YouTube
        reported that the channel does not exist (404 SOURCE_NOT_FOUND),
        False for any other failure (503 PROVIDER_UNAVAILABLE).
        `reason` is yt-dlp's own message, for the server-side log only
        -- never put into a response body.
    '''

    def __init__(self, reason, *, not_found):
        super().__init__(reason)
        self.reason = reason
        self.not_found = not_found


def parse_channel_handle_url(url):
    '''
        Returns (lookup_url, None) for an accepted URL shape, or
        (None, error_message) otherwise. The error message never echoes
        the caller's URL.

        Query string and fragment are ignored (never forwarded -- the
        lookup URL is rebuilt from the path alone): YouTube's own share
        links for a channel carry `?si=...`, and dropping it is safe for
        exactly that reason.
    '''
    candidate = url.strip()
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None, 'url is not a valid URL.'
    if parts.scheme.lower() != 'https':
        return None, 'url must use https.'
    if '@' in parts.netloc:
        return None, 'url must not carry a username or password.'
    if parts.netloc.lower() not in ALLOWED_HOSTS:
        return None, (
            'url host must be one of '
            f'{sorted(ALLOWED_HOSTS)!r} (no port).'
        )
    match = _PATH_PATTERN.fullmatch(parts.path)
    if not match:
        return None, _SHAPE_ERROR
    if match.group('handle'):
        return f'https://www.youtube.com/@{match.group("handle")}', None
    if match.group('custom'):
        return f'https://www.youtube.com/c/{match.group("custom")}', None
    return f'https://www.youtube.com/user/{match.group("user")}', None


# Same bound views_resolve applies to its own lookup-failure log line.
_YT_DLP_LOG_MAX_LENGTH = 500


def _clean_log_message(msg):
    return sanitize_error_message(str(msg or ''))[:_YT_DLP_LOG_MAX_LENGTH]


class _YtDlpLogger:
    '''
        yt-dlp reports a failed extraction through logger.error() before
        raising DownloadError. A missing channel or a transient YouTube
        failure is an expected outcome for this endpoint, not a bridge
        error, so it is logged at warning; yt-dlp's progress chatter goes
        to debug. Every message is sanitized and bounded first, since
        yt-dlp's text can carry the cookie path or credential-shaped
        diagnostics.
    '''

    def debug(self, msg):
        log.debug('medianest_bridge: resolve yt-dlp: %s', _clean_log_message(msg))

    def info(self, msg):
        log.debug('medianest_bridge: resolve yt-dlp: %s', _clean_log_message(msg))

    def warning(self, msg):
        log.warning('medianest_bridge: resolve yt-dlp: %s', _clean_log_message(msg))

    def error(self, msg):
        log.warning('medianest_bridge: resolve yt-dlp: %s', _clean_log_message(msg))


def _lookup_opts():
    from sync.youtube import get_yt_opts
    opts = get_yt_opts()
    opts.update({
        'skip_download': True,
        'simulate': True,
        'logger': _YtDlpLogger(),
        'extract_flat': True,
        'check_formats': False,
        'playlist_items': '1',
        'socket_timeout': LOOKUP_SOCKET_TIMEOUT_SECONDS,
        'extractor_retries': LOOKUP_EXTRACTOR_RETRIES,
    })
    return opts


def _iter_error_chain(error):
    '''
        DownloadError -> exc_info[1] (usually an ExtractorError) ->
        .cause / __cause__ (e.g. a networking HTTPError), bounded.
    '''
    seen = set()
    pending = [error]
    while pending and len(seen) < 10:
        current = pending.pop(0)
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        exc_info = getattr(current, 'exc_info', None)
        if isinstance(exc_info, tuple) and len(exc_info) > 1:
            pending.append(exc_info[1])
        pending.append(getattr(current, 'cause', None))
        pending.append(getattr(current, '__cause__', None))


# Substrings of yt-dlp 2026.08.19's own messages that mean "YouTube has
# no such channel" (see yt_dlp/extractor/youtube/_tab.py and _base.py):
# - 'HTTP Error 404' -- networking.exceptions.HTTPError's message, inside
#   "Unable to download API page: HTTP Error 404: Not Found" when
#   navigation/resolve_url (or the webpage) 404s for an unknown handle;
# - 'does not exist' -- "The channel/playlist does not exist and the URL
#   redirected to youtube.com home page", and YouTube's own alert
#   "YouTube said: This channel does not exist.";
# - 'does the playlist exist?' -- "Failed to resolve url (does the
#   playlist exist?)", resolve_url answering with no browse endpoint.
_NOT_FOUND_MARKERS = (
    'http error 404',
    'does not exist',
    'does the playlist exist?',
)


def _is_not_found(error):
    for link in _iter_error_chain(error):
        if getattr(link, 'status', None) == 404:
            return True
    message = str(error).lower()
    return any(marker in message for marker in _NOT_FOUND_MARKERS)


def _display_name(response):
    for field in ('channel', 'uploader', 'title'):
        value = response.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()[:DISPLAY_NAME_MAX_LENGTH]
    return None


def lookup_channel_identity(lookup_url):
    '''
        Returns (channel_id, display_name). channel_id is whatever yt-dlp
        reported (possibly None or malformed -- the caller validates it
        against CHANNEL_ID_PATTERN); display_name is the channel title,
        preferring `channel`, then `uploader`, then `title`, stripped and
        truncated to 200 characters, or None when none is set.

        Raises ChannelLookupError on a yt-dlp DownloadError.
    '''
    with yt_dlp.YoutubeDL(_lookup_opts()) as ydl:
        try:
            response = ydl.extract_info(lookup_url, download=False)
        except yt_dlp.utils.DownloadError as e:
            raise ChannelLookupError(str(e), not_found=_is_not_found(e)) from e
    if not isinstance(response, dict):
        return None, None
    channel_id = response.get('channel_id')
    if not isinstance(channel_id, str):
        channel_id = None
    return channel_id, _display_name(response)
