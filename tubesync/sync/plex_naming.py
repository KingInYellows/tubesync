'''
    Fork-local Plex naming helpers (MED-702).

    Artist-first episode titles: `artist_title()` turns a video title into
    "Artist - Title" for sources whose `media_format` uses
    `{artist_title}` or `{artist_title_bounded}`; `Media.nfoxml` then uses
    the same text for the episode's <title>, so the file name and Plex
    agree.
'''


# media_format keys that opt a source into artist-first episode titles.
ARTIST_TITLE_FIELDS = frozenset(('artist_title', 'artist_title_bounded'))

# Separators that end the artist part of a title, as YouTube music titles
# write them. Not ": " -- too common inside song and show names.
_TITLE_SEPARATORS = (' - ', ' – ', ' — ', ' | ')


def _split_title(title):
    '''(artist part, rest) at the earliest separator, or (None, None).'''
    best = None
    for sep in _TITLE_SEPARATORS:
        idx = title.find(sep)
        if idx > 0 and (best is None or idx < best[0]):
            best = (idx, sep)
    if best is None:
        return None, None
    idx, sep = best
    left, right = title[:idx].strip(), title[idx + len(sep):].strip()
    if not left or not right:
        return None, None
    return left, right


def _starts_with_name(text, names):
    '''
        True when `text` begins with one of `names` as a whole name
        ("Hans Zimmer & Lisa Gerrard" for "Lisa Gerrard" is False, for
        "Hans Zimmer" True; "My Heart Will Go On" for "Heart" is False).
    '''
    folded = text.casefold()
    for name in names:
        name = name.casefold()
        if folded.startswith(name) and (
            len(folded) == len(name) or not folded[len(name)].isalnum()
        ):
            return True
    return False


def artist_title(title, artists=()):
    '''
        "Artist - Title" for an episode title:
          - with artists from the video's metadata: a title that already
            starts with one of them (as a whole name) is kept, with its separator made " - " ("Hans Zimmer |
            Live in Cologne" -> "Hans Zimmer - Live in Cologne"); any other
            title is prefixed ("Live in Cologne" -> "Hans Zimmer - Live in
            Cologne");
          - without one: a title already shaped "Artist <sep> Title" gets
            its first separator made " - "; anything else is left as is,
            since a channel name is too often not the artist (a venue, a
            label, a TV show) to stand in for one.
    '''
    title = str(title or '').strip()
    names = [str(a).strip() for a in (artists or ()) if str(a or '').strip()]
    left, right = _split_title(title)
    if names:
        if left is not None and _starts_with_name(left, names):
            return f'{left} - {right}'
        if _starts_with_name(title, names):
            return title
        artist = ', '.join(names)
        return f'{artist} - {title}' if title else artist
    if left is not None:
        return f'{left} - {right}'
    return title


def metadata_artists(media):
    '''
        The video's artist names from yt-dlp metadata: `artists` (a list),
        else `artist` (a comma-joined string). Empty when YouTube has no
        music credit for the video.
    '''
    for key in ('artists', 'artist'):
        value = media.get_metadata_first_value(key)
        names = []
        if isinstance(value, (list, tuple)):
            names = [str(v).strip() for v in value if str(v or '').strip()]
        elif isinstance(value, str):
            names = [part.strip() for part in value.split(',') if part.strip()]
        if names:
            return names
    return []
