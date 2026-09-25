'''
    Writes a Kodi/Plex "tvshow.nfo" (show-level NFO) for a Source, resolving
    a display title/plot from the cheapest real data available rather than
    always falling back to `Source.name`.

    This is deliberately separate from the upstream
    `sync/management/commands/create-tvshow-nfo.py` command, which is left
    untouched (F5): that command is a manual, one-shot, never-overwrite tool
    nothing in the tree calls, and it builds its XML from an unescaped
    f-string (a literal "&" in a channel name breaks the file). This module
    is the automatic, escaped (via `ElementTree` + `clean_emoji`, matching
    `Media.nfoxml`'s own approach), idempotent writer hooked into
    `index_source`/`download_source_images` (`sync/tasks.py`).
'''
from collections import OrderedDict
from pathlib import Path
from xml.etree import ElementTree

from django.db.models import F

from common.logger import log
from common.utils import clean_emoji

from .models._private import _nfo_element
from .utils import write_text_file


def _cached_channel_metadata(source):
    '''
        Returns the `Metadata` row `sync/youtube.py:get_image_info` caches
        for this source's channel/playlist entity (`source`/`media` both
        NULL, keyed by the extractor's own id for that entity) via
        `Source.get_image_url` (F6), or `None` if nothing is cached yet --
        that cache is only populated once `download_source_images` has run
        at least once for this source.

        This only matches directly for PLAYLIST and CHANNEL_ID sources,
        whose `Source.key` already IS that extractor id. For a handle-based
        CHANNEL source (`key` is the handle, e.g. "@somechannel"), the
        cached row's key is the resolved "UC..." id yt-dlp returns for the
        channel, which this function has no cheap (no extra network call)
        way to learn. `resolve_show_title()`/`resolve_show_studio()` simply
        fall through to their next tier in that case -- an accepted,
        documented limitation, not a bug.
    '''
    # Deferred import: `sync.models` is mid-import when `sync.models.media`
    # (which calls into this module) is first loaded -- see
    # `sync/models/__init__.py`'s import order (`Media` before `Metadata`).
    from .models import Metadata
    return Metadata.objects.filter(
        source__isnull=True,
        media__isnull=True,
        key=source.key,
    ).order_by('-retrieved').first()


def _resolve_show_title_from_data(source, cached):
    '''
        Resolves a show title/studio from real channel/media data only (no
        `source.name` fallback) -- shared by `resolve_show_title()` and
        `resolve_show_studio()`, which should not just repeat `source.name`
        when nothing more informative is available. `cached` is
        `_cached_channel_metadata(source)`, passed in so a caller that
        needs it for several fields queries it once. Returns `None` when
        nothing was found. Tries, in order:
        1. The cached channel/playlist `Metadata` row's own `title`.
        2. The media with metadata and the most recent `published` date
           (NULL `published` sorts last, tie-broken by `-created`): its
           `playlist_title` (for a playlist source) or `channel`/`uploader`
           (for a channel source). Media without metadata are skipped --
           they cannot supply either value.

        Not cached: TubeSync's tasks run via huey, potentially across more
        than one worker process, so a naive process-local cache would not
        reliably stay fresh or even be shared between the process that last
        refreshed it and the one rendering a given episode's NFO. Both
        queries here are either a unique-key lookup or an already-indexed,
        `LIMIT 1` query -- cheap enough per item that this was judged not
        worth that risk. (Flagged for the reviewer: `Media.nfoxml` calls
        this once per episode, including from inside
        `rename_all_media_for_source`'s loop over every downloaded item of
        a source -- if that ever shows up as a real cost, a cache keyed by
        `source.pk` with `write_tvshow_nfo()` as its one refresh point
        would be the place to add it.)
    '''
    if cached is not None:
        title = str(cached.value.get('title', '') or '').strip()
        if title:
            return title
    latest_media = source.media_source.filter(
        metadata__isnull=False,
    ).order_by(F('published').desc(nulls_last=True), '-created').first()
    if latest_media is not None:
        if source.is_playlist:
            title = str(latest_media.playlist_title or '').strip()
        else:
            title = str(
                latest_media.get_metadata_first_value(('channel', 'uploader')) or ''
            ).strip()
        if title:
            return title
    return None


def _plot_from(cached):
    if cached is not None:
        return str(cached.value.get('description', '') or '').strip()
    return ''


def resolve_show_title(source):
    '''
        Best available display title for a source's tvshow.nfo <title> (and
        the episode NFO's <showtitle>): `_resolve_show_title_from_data`,
        falling back to `source.name` (TubeSync's own local, always-present
        name) when nothing more informative is known yet.
    '''
    cached = _cached_channel_metadata(source)
    return _resolve_show_title_from_data(source, cached) or source.name


def resolve_show_studio(source):
    '''
        <studio> is only set when `_resolve_show_title_from_data` finds a
        real name -- for a channel that is the channel/uploader name, for
        a playlist it is the playlist's own title (the same value as
        <title>). Studio duplicating `source.name` with no more
        information than <title> already carries is not worth adding.
    '''
    return _resolve_show_title_from_data(source, _cached_channel_metadata(source))


def resolve_show_plot(source):
    '''
        Best available <plot> for tvshow.nfo: the cached channel/playlist
        Metadata row's own `description`, when known (see
        `_cached_channel_metadata`'s caveats); `''` otherwise. There is no
        per-media fallback for this one, unlike title -- a single video's
        description is not a meaningful stand-in for a whole channel's.
    '''
    return _plot_from(_cached_channel_metadata(source))


def build_tvshow_nfo(source):
    '''
        Returns a Kodi/Plex "tvshow.nfo" formatted (prettified) XML string
        for `source`. Looks the channel cache and latest media up once and
        derives <title>, <studio> and <plot> from that one snapshot.
    '''
    cached = _cached_channel_metadata(source)
    studio = _resolve_show_title_from_data(source, cached)
    title = clean_emoji(studio or source.name)
    plot = clean_emoji(_plot_from(cached))
    nfo = ElementTree.Element('tvshow')
    nfo.text = '\n  '
    nfo.append(_nfo_element(nfo, 'title', title))
    nfo.append(_nfo_element(nfo, 'sorttitle', title))
    if plot:
        nfo.append(_nfo_element(nfo, 'plot', plot))
    uniqueid_attrs = OrderedDict()
    uniqueid_attrs['type'] = 'youtube'
    uniqueid_attrs['default'] = 'true'
    nfo.append(_nfo_element(
        nfo, 'uniqueid', str(source.key).strip(), attrs=uniqueid_attrs,
    ))
    if studio:
        nfo.append(_nfo_element(nfo, 'studio', clean_emoji(studio)))
    nfo[-1].tail = '\n'
    return ElementTree.tostring(nfo, encoding='utf8', method='xml').decode('utf8')


def write_tvshow_nfo(source):
    '''
        Writes `tvshow.nfo` for `source`, only when `write_nfo` is enabled.
        Idempotent: skips the filesystem write entirely when the bytes on
        disk already match what would be written, so calling this from
        `index_source`, `download_source_images` and
        `download_media_metadata` every run does not churn the file (or
        its mtime) when nothing has changed. Never deletes anything.

        Best-effort: it runs at the tail of those tasks, after their real
        work has succeeded, so it never raises. A missing source directory
        (not created yet by `check_source_directory_exists`) is skipped --
        creating it is not this function's job -- and any other error is
        logged with its traceback instead of failing, and so retrying, the
        calling task.
    '''
    if not source.write_nfo:
        return
    try:
        directory = Path(source.directory_path)
        if not directory.is_dir():
            log.debug(f'Skipping tvshow.nfo, no directory yet for: {source}')
            return
        nfo_path = directory / 'tvshow.nfo'
        content = build_tvshow_nfo(source)
        if nfo_path.exists() and nfo_path.read_bytes() == content.encode('utf-8'):
            return
        log.info(f'Writing tvshow.nfo for: {source}')
        write_text_file(nfo_path, content)
    except Exception:
        log.exception(f'Failed to write tvshow.nfo for: {source}')
