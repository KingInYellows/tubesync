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
    `index_source`, `download_source_images` and `download_media_metadata`
    (`sync/tasks.py`).
'''
import time
from collections import OrderedDict
from pathlib import Path
from xml.etree import ElementTree

from django.db import DatabaseError
from django.db.models import F

from common.logger import log
from common.utils import clean_emoji

from .models._private import _nfo_element
from .utils import write_text_file


# resolve_show_title()'s process-local cache: source.pk -> (expires_at, title)
# where expires_at is a `time.monotonic()` deadline. See resolve_show_title()
# for the caching rationale.
_SHOW_TITLE_CACHE_TTL_SECONDS = 60
_show_title_cache = {}


def _clear_show_title_cache():
    '''
        Empties the process-local resolve_show_title() cache. Also used by
        tests (in `setUp`) so a cached entry from one test/source.pk cannot
        leak into another and make results depend on run order.
    '''
    _show_title_cache.clear()


def _invalidate_show_title_cache(source):
    '''
        Drops `source`'s cached resolve_show_title() entry, if any. Called
        by `write_tvshow_nfo()` whenever it recomputes the show's data from
        scratch, so a stale cached title cannot outlive the fresher one
        `write_tvshow_nfo()` just derived. A no-op for an unsaved source
        (`pk` is `None`), which `resolve_show_title()` never caches.
    '''
    if source.pk is not None:
        _show_title_cache.pop(source.pk, None)


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
           (for a channel source). Media without metadata (in neither the
           legacy `metadata` column nor the related `new_metadata` row)
           are skipped -- they cannot supply either value.

        Playlist tier-2 caveat (accepted limitation): per-media metadata is
        fetched from standalone `watch?v=` URLs, so `playlist_title` is
        usually empty even for playlist sources. We do not read
        `source.videos` (the indexed playlist payload) here -- that would
        couple this resolver to the index payload shape for a
        fallback-of-a-fallback. Bridge-created sources normally never hit
        this path: their profile sets `copy_channel_images=True`, so tier 1
        caches the playlist-scoped metadata; when tier 1/2 both miss,
        `resolve_show_title()` falls back to `source.name` (MediaNest sets
        that from the playlist title at creation).

        Not cached here: this helper is also called directly by
        `build_tvshow_nfo()`, which always wants a fresh read. The caching
        lives one layer up, in `resolve_show_title()` -- see its docstring.
        Both queries here are either a unique-key lookup or an
        already-indexed, `LIMIT 1` query -- cheap enough for that caller's
        one-per-write cost.
    '''
    if cached is not None:
        title = str(cached.value.get('title', '') or '').strip()
        if title:
            return title
    latest_media = source.media_source.exclude(
        metadata__isnull=True, new_metadata__isnull=True,
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

        Cached for `_SHOW_TITLE_CACHE_TTL_SECONDS` (60s), process-locally,
        keyed by `source.pk`. `Media.nfoxml` calls this once per episode,
        including from inside `rename_all_media_for_source`'s loop over
        every downloaded item of a source -- uncached, that is 1-3 extra
        queries (`_cached_channel_metadata` plus the "latest media" lookup)
        per item. TubeSync's tasks run via huey, potentially across more
        than one worker process, so this cache is not shared or invalidated
        across processes -- a stale title can survive up to the TTL in a
        worker that is not the one `write_tvshow_nfo()` last ran in. That
        bounded staleness (one channel-name change, one worker, 60 seconds)
        is judged an acceptable trade for avoiding a shared cache's
        complexity; `write_tvshow_nfo()` invalidates its own process's entry
        immediately whenever it recomputes (see
        `_invalidate_show_title_cache`), so the common case -- one worker,
        one source, indexed then downloaded -- always sees a fresh value.

        An unsaved source (`pk` is `None`) bypasses the cache entirely: it
        has no stable key to cache under, and resolving it twice is rare
        (nothing calls this before a source is saved in normal operation).

        Never raises on a database error: `DatabaseError` is caught, logged
        with a traceback, and `source.name` is returned instead -- a lookup
        failure here must not fail the caller. `Media.nfoxml` is invoked
        from `write_nfo_file` (`sync/models/media__tasks.py`), which upstream
        only guards against `PermissionError`, and `download_media_file`
        calls it after the video has already downloaded; letting a DB error
        propagate from here would fail an otherwise-complete download task.
        A fallback produced by an error is deliberately not cached, so the
        next call retries the real lookup rather than pinning the fallback
        for the TTL.
    '''
    if source.pk is not None:
        cached_entry = _show_title_cache.get(source.pk)
        if cached_entry is not None:
            expires_at, title = cached_entry
            if time.monotonic() < expires_at:
                return title
    try:
        cached = _cached_channel_metadata(source)
        title = _resolve_show_title_from_data(source, cached) or source.name
    except DatabaseError:
        log.exception(f'Failed to resolve show title for: {source}')
        return source.name
    if source.pk is not None:
        _show_title_cache[source.pk] = (
            time.monotonic() + _SHOW_TITLE_CACHE_TTL_SECONDS, title,
        )
    return title


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

        Carries two `<uniqueid>` elements: `type="youtube"` (the source's
        current, user-editable `key` -- the one Kodi/Plex/Jellyfin actually
        scrape against) and a second, non-`default` `type="tubesync"` (the
        source's immutable `uuid` primary key). Extra `<uniqueid>` elements
        are ignored by those scrapers as long as none but the intended one
        is `default="true"`. The second one lets `_foreign_nfo_reason`
        keep recognising a file this writer created even after the user
        edits the source's `key` through the source-update form, which
        would otherwise leave a file with a stale youtube id (and its
        stale title/plot) permanently un-owned and un-refreshed.
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
    ownership_attrs = OrderedDict()
    ownership_attrs['type'] = 'tubesync'
    nfo.append(_nfo_element(
        nfo, 'uniqueid', str(source.uuid), attrs=ownership_attrs,
    ))
    if studio:
        nfo.append(_nfo_element(nfo, 'studio', clean_emoji(studio)))
    nfo[-1].tail = '\n'
    return ElementTree.tostring(nfo, encoding='utf8', method='xml').decode('utf8')


def _foreign_nfo_reason(nfo_path, source):
    '''
        Why the file at `nfo_path` is not this writer's to replace, or None
        when it is (absent, empty, or a `<tvshow>` carrying either this
        source's `<uniqueid type="youtube">` (current `key`) or its
        `<uniqueid type="tubesync">` (immutable `uuid` primary key) --
        only this writer emits either):
          - another root, such as a video's own `<episodedetails>` from a
            `media_format` that renders a filename as `tvshow`; overwriting
            it would leave the two writers replacing each other's file;
          - any other `<tvshow>`, such as one written by hand or by the
            upstream `create-tvshow-nfo` command, which never overwrites;
          - a non-empty file that does not parse as XML at all, such as a
            Kodi URL-only/combination NFO (a bare channel URL, no markup)
            or upstream `create-tvshow-nfo`'s own output when a channel
            name has a raw, unescaped "&" (the bug F5 left that command
            for) -- both are real, intentionally-placed files this writer
            must not silently clobber just because it cannot parse them.
            A zero-byte file is not treated this way: it carries no
            content to protect, so it is still replaceable, same as an
            absent one.

        The `tubesync` id is checked so that editing a source's `key`
        through the source-update form does not orphan the file this
        writer already created for it: matching by key alone would treat
        it as foreign forever after, freezing its title/plot stale. A file
        written before the `tubesync` id existed only carries the youtube
        one, which still matches as long as `key` has not since changed --
        that older/unedited case is unaffected.
    '''
    if not nfo_path.exists():
        return None
    raw = nfo_path.read_bytes()
    if not raw:
        return None
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        return 'it exists but could not be parsed as XML'
    if root.tag != 'tvshow':
        return (
            'it holds another NFO (does media_format render a video '
            'filename as "tvshow"?)'
        )
    key = str(source.key).strip()
    tubesync_id = str(source.uuid)
    for uniqueid in root.iter('uniqueid'):
        uid_type = uniqueid.get('type')
        uid_text = (uniqueid.text or '').strip()
        if uid_type == 'youtube' and uid_text == key:
            return None
        if uid_type == 'tubesync' and uid_text == tubesync_id:
            return None
    return 'it is a tvshow.nfo this writer did not create'


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

        Every call recomputes the show's data from scratch and, having done
        so, drops this source's `resolve_show_title()` cache entry (see
        `_invalidate_show_title_cache`) -- this is that cache's one refresh
        point, so a subsequent `resolve_show_title()`/`Media.nfoxml` call in
        this process picks up the fresh value immediately rather than
        waiting out the TTL.

        Concurrent-write caveat (accepted limitation): two
        `download_media_metadata` tasks for the same source can finish
        concurrently and, if the resolved show title changes between one
        worker's `build_tvshow_nfo()` and its `write_text_file()`, a stale
        snapshot can briefly replace a fresher `tvshow.nfo`. That lost
        update needs both tasks to overlap on the same source and the title
        to change between them -- in practice this is a one-time window
        when the first real channel name appears. `write_text_file()` is
        atomic (temp file plus replace), so the file is never corrupt.
        Every later `download_media_metadata`, `index_source` or
        `download_source_images` run rebuilds from current data and
        rewrites a stale file when the bytes differ. A per-source lock on
        the huey DB queue would serialise every metadata task of a source
        just to close that narrow window, so we accept this self-healing
        race instead.
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
        _invalidate_show_title_cache(source)
        if nfo_path.exists() and nfo_path.read_bytes() == content.encode('utf-8'):
            return
        reason = _foreign_nfo_reason(nfo_path, source)
        if reason:
            log.warning(f'Not writing tvshow.nfo for: {source}: {reason}')
            return
        log.info(f'Writing tvshow.nfo for: {source}')
        write_text_file(nfo_path, content)
    except Exception:
        log.exception(f'Failed to write tvshow.nfo for: {source}')
