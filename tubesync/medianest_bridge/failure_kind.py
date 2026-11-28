'''
    Contract 1.6.0 (MED-813, MediaNest ADR-0038): MediaItem.failureKind,
    the bridge's reading of why a download failed, so MediaNest can say it
    in plain words ("This video is private on YouTube") instead of showing
    a cleaned-up yt-dlp error.

    classify() matches the error text TubeSync stored for the failed task
    (TaskHistory.last_error) against the phrases yt-dlp and the OS use for
    each cause. Order matters: the first group that matches wins, and the
    specific causes come before `temporary`, since a removed video can
    also surface as an HTTP error. Anything else is `unknown`, never a
    guess.

    The phrases are yt-dlp's own messages (yt_dlp/extractor/youtube),
    matched case-insensitively as plain substrings. A new yt-dlp wording
    only means `unknown` until a phrase is added here; the error string
    itself still reaches the caller.
'''

_KINDS = (
    ('disk_full', (
        'no space left on device',
        '[errno 28]',
        'disk quota exceeded',
    )),
    ('private', (
        'private video',
        'this video is private',
        'video is private',
    )),
    ('members_only', (
        'members-only',
        'members only',
        "available to this channel's members",
        'join this channel to get access',
    )),
    ('age_restricted', (
        'sign in to confirm your age',
        'age-restricted',
        'age restricted',
        'inappropriate for some users',
    )),
    ('geo_blocked', (
        'not available in your country',
        'blocked it in your country',
        'available in your country',
        'geo restriction',
        'geo-restricted',
        'georestricted',
    )),
    ('upcoming', (
        'premieres in',
        'this live event will begin',
        'live event will begin in',
        'is_upcoming',
        'premiere will begin',
    )),
    ('unavailable', (
        'video unavailable',
        'this video is unavailable',
        'this video is no longer available',
        'this video has been removed',
        'has been terminated',
        'account associated with this video',
        'violating youtube',
        'copyright claim',
    )),
    ('temporary', (
        "sign in to confirm you're not a bot",
        'sign in to confirm you’re not a bot',
        'http error 403',
        'http error 429',
        'http error 5',
        'too many requests',
        'timed out',
        'timeout',
        'connection reset',
        'connection refused',
        'connection aborted',
        'network is unreachable',
        '[errno 101]',
        '[errno 104]',
        '[errno 110]',
        '[errno 111]',
        'temporary failure in name resolution',
        'unable to download webpage',
        'requested format is not available',
        'no video formats found',
        'metadata is not yet available',
        'fragment not found',
        'giving up after',
    )),
)

KNOWN_KINDS = tuple(kind for kind, _ in _KINDS) + ('unknown',)


def classify(error_text):
    '''The failureKind for an error text; `unknown` when nothing matches.'''
    text = (error_text or '').lower()
    if not text:
        return 'unknown'
    for kind, phrases in _KINDS:
        if any(phrase in text for phrase in phrases):
            return kind
    return 'unknown'
