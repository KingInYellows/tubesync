'''
    Fork-local Plex naming helpers (MED-702).

    1. Season posters. Plex's local media assets for TV read a season
       poster from inside the season folder, named `SeasonXX.ext` where XX
       is the season number (support.plex.tv/articles/
       200220717-local-media-assets-tv-shows/). The NFO agent prefers those
       local assets (support.plex.tv/articles/
       using-nfo-metadata-files-with-plex/). TubeSync's own
       `season-poster.jpg` sits in the show folder, which Plex does not
       read, so each `Season YYYY/` folder gets a `SeasonYYYY.jpg` copied
       from it. A season poster is only ever created, never replaced, so a
       hand-placed one stays; delete it to have it copied again.

    2. Artist-first episode titles. `artist_title()` turns a video title
       into "Artist - Title" for sources whose `media_format` uses
       `{artist_title}` or `{artist_title_bounded}`; `Media.nfoxml` then
       uses the same text for the episode's <title>.
'''
import re
from pathlib import Path

from common.logger import log


# `Season 2026` (the bridge profile's `Season {episode_yyyy}/`), also
# `Season 01` / `season 1`.
SEASON_DIR_RE = re.compile(r'^season (\d+)$', re.IGNORECASE)

# Show-folder images a season poster is copied from, best first.
# download_source_images writes both; season-poster.jpg is the one meant
# for seasons (and the one MEDIA-OPS replaced by hand for custom art).
SEASON_POSTER_SOURCES = ('season-poster.jpg', 'poster.jpg')

# media_format keys that opt a source into artist-first episode titles.
ARTIST_TITLE_FIELDS = frozenset(('artist_title', 'artist_title_bounded'))

# Separators that end the artist part of a title, as YouTube music titles
# write them. Not ": " -- too common inside song and show names.
_TITLE_SEPARATORS = (' - ', ' – ', ' — ', ' | ')


def season_poster_name(season_number):
    '''`Season2026.jpg`, `Season01.jpg`: Plex's documented SeasonXX name.'''
    return f'Season{int(season_number):02d}.jpg'


def season_number_of(directory):
    '''The season number a `Season NN` directory name carries, or None.'''
    match = SEASON_DIR_RE.match(Path(directory).name)
    return int(match.group(1)) if match else None


def _show_poster(show_dir):
    for name in SEASON_POSTER_SOURCES:
        path = show_dir / name
        try:
            if path.is_file() and not path.is_symlink() and path.stat().st_size > 0:
                return path
        except OSError:
            continue
    return None


def ensure_season_poster(show_dir, season_dir, *, apply_changes=True):
    '''
        Copies the show's season poster into `season_dir` as
        `SeasonNN.jpg` when `season_dir` is a direct `Season NN` child of
        `show_dir` with no such file yet. Returns the path written (or that
        would be written when `apply_changes` is False), else None. Never
        overwrites anything, follows no symlink at the target, and never
        raises on a filesystem error (it is logged): a missing season
        poster must not fail a download.
    '''
    show_dir = Path(show_dir)
    season_dir = Path(season_dir)
    number = season_number_of(season_dir)
    if number is None or season_dir.parent != show_dir:
        return None
    target = season_dir / season_poster_name(number)
    try:
        if not season_dir.is_dir() or season_dir.is_symlink():
            return None
        if target.exists() or target.is_symlink():
            return None
        source_image = _show_poster(show_dir)
        if source_image is None:
            return None
        if apply_changes:
            data = source_image.read_bytes()
            # 'xb' (O_EXCL): never replace a file that appeared meanwhile
            try:
                with open(target, 'xb') as f:
                    f.write(data)
            except FileExistsError:
                return None
            log.info(f'Wrote season poster: {target}')
        return target
    except OSError as e:
        log.warning(f'Could not write season poster {target}: {e}')
        return None


def write_season_posters(show_dir, *, apply_changes=True):
    '''
        `ensure_season_poster()` for every `Season NN` folder directly in
        `show_dir`. Returns the list of paths written (or that would be).
    '''
    show_dir = Path(show_dir)
    written = []
    try:
        children = sorted(show_dir.iterdir())
    except OSError as e:
        log.warning(f'Could not list {show_dir} for season posters: {e}')
        return written
    for child in children:
        path = ensure_season_poster(show_dir, child, apply_changes=apply_changes)
        if path is not None:
            written.append(path)
    return written


def ensure_media_season_poster(media):
    '''
        After a download or rename: the season poster for the folder the
        media's file is in, when that is a `Season NN` folder of its
        source. Never raises.
    '''
    try:
        if not media.media_file:
            return None
        return ensure_season_poster(
            Path(media.source.directory_path),
            Path(media.media_file.path).parent,
        )
    except Exception as e:
        log.warning(f'Season poster check failed for {media}: {e}')
        return None


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


def artist_title(title, artists=()):
    '''
        "Artist - Title" for an episode title:
          - with an artist from the video's metadata (`artists`, first
            name used to match): a title that already names that artist
            first is kept, with its separator made " - " ("Hans Zimmer |
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
        first = names[0].casefold()
        if left is not None and first in left.casefold():
            return f'{left} - {right}'
        if title.casefold().startswith(first):
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
    value = media.get_metadata_first_value(('artists', 'artist'))
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v or '').strip()]
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in value.split(',') if part.strip()]
    return []
