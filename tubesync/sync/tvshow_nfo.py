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
import hashlib
import re
import threading
import time
from collections import OrderedDict
from xml.etree import ElementTree

from django import db
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
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
# source.pk -> how many times write_tvshow_nfo() invalidated it. A lookup
# that started before an invalidation must not store its (older) result.
_show_title_generations = {}
_show_title_lock = threading.Lock()

# How many of a source's most recently published media (with metadata) the
# title lookup reads before giving up on the per-media tier. A freshly
# indexed stub row can carry metadata without a channel/uploader name.
_TITLE_MEDIA_SCAN_LIMIT = 5

# The writer's own marker: `<uniqueid type="tubesync" checksum="sha256:...">`
# holding the source's immutable uuid. The checksum covers the whole file
# with the checksum value itself blanked, so a hand edit is detectable.
_CHECKSUM_RE = re.compile(
    r'(<uniqueid type="tubesync" checksum=")sha256:([0-9a-f]{64})(")',
)
_CHECKSUM_PLACEHOLDER = '<uniqueid type="tubesync" checksum=""'


def _clear_show_title_cache():
    '''
        Empties the process-local resolve_show_title() cache. Also used by
        tests (in `setUp`) so a cached entry from one test/source.pk cannot
        leak into another and make results depend on run order.
    '''
    with _show_title_lock:
        _show_title_cache.clear()
        _show_title_generations.clear()


def _invalidate_show_title_cache(source):
    '''
        Drops `source`'s cached resolve_show_title() entry, if any, and
        bumps its generation so a lookup already in flight does not store
        its older result. Called by `write_tvshow_nfo()` whenever it
        recomputes the show's data from scratch. A no-op for an unsaved
        source (`pk` is `None`), which `resolve_show_title()` never caches.
    '''
    if source.pk is None:
        return
    with _show_title_lock:
        _show_title_cache.pop(source.pk, None)
        _show_title_generations[source.pk] = (
            _show_title_generations.get(source.pk, 0) + 1
        )


def _store_show_title(source, generation, title):
    '''
        Caches `title` for `source` unless it was invalidated since the
        lookup read `generation`. Expired entries are dropped on the way,
        so the cache never holds more than the sources used in the last
        TTL.
    '''
    now = time.monotonic()
    with _show_title_lock:
        if _show_title_generations.get(source.pk, 0) != generation:
            return
        for key, (expires_at, _) in list(_show_title_cache.items()):
            if expires_at <= now:
                del _show_title_cache[key]
        _show_title_cache[source.pk] = (
            now + _SHOW_TITLE_CACHE_TTL_SECONDS, title,
        )


def _cached_channel_metadata(source):
    '''
        Returns the `Metadata` row `sync/youtube.py:get_image_info` caches
        for this source's channel/playlist entity (`source`/`media` both
        NULL, keyed by the extractor's own id for that entity) via
        `Source.get_image_url` (F6), or `None` if nothing is cached yet --
        that cache is only populated once `download_source_images` has run
        at least once for this source. `get_image_info` is the only writer
        of rows with both `source` and `media` NULL, so the key alone
        identifies it; `site` (the extractor key, `YoutubeTab`) is not
        filtered on.

        This only matches directly for PLAYLIST and CHANNEL_ID sources,
        whose `Source.key` already IS that extractor id. For a handle-based
        CHANNEL source (`key` is the handle, e.g. "@somechannel"), the
        cached row's key is the resolved "UC..." id yt-dlp returns for the
        channel, which this function has no cheap (no extra network call)
        way to learn. `resolve_show_title()` simply falls through to its
        next tier in that case -- an accepted, documented limitation, not
        a bug.
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


def _clean_text(value):
    '''`value` as text with emoji removed and whitespace stripped.'''
    return clean_emoji(str(value or '')).strip()


def _media_title(source):
    '''
        (title, retrieved) from the most recently published media that
        carries one: its `playlist_title` for a playlist source, else its
        `channel`/`uploader`. Reads up to `_TITLE_MEDIA_SCAN_LIMIT` rows
        with metadata (in either the legacy `metadata` column or the
        related `new_metadata` row), newest `published` first (NULL last,
        then `-created`), so a stub row without the value does not hide
        one a slightly older row has. `retrieved` is that media's
        `new_metadata.retrieved`, or `None` when unknown. `(None, None)`
        when none of them has one.
    '''
    candidates = source.media_source.exclude(
        metadata__isnull=True, new_metadata__isnull=True,
    ).order_by(
        F('published').desc(nulls_last=True), '-created',
    )[:_TITLE_MEDIA_SCAN_LIMIT]
    for media in candidates:
        if source.is_playlist:
            title = _clean_text(media.playlist_title)
        else:
            title = _clean_text(
                media.get_metadata_first_value(('channel', 'uploader')),
            )
        if title:
            try:
                retrieved = media.new_metadata.retrieved
            except ObjectDoesNotExist:
                retrieved = None
            return title, retrieved
    return None, None


def _resolve_show_title_from_data(source, cached):
    '''
        Resolves a show title/studio from real channel/media data only (no
        `source.name` fallback), emoji removed and stripped -- used by
        `resolve_show_title()` and `build_tvshow_nfo()`, which should not
        just repeat `source.name` as <studio> when nothing more
        informative is available. `cached` is
        `_cached_channel_metadata(source)`, passed in so a caller that
        needs it for several fields queries it once. Returns `None` when
        nothing was found. Two tiers:
        1. The cached channel/playlist `Metadata` row: for a channel its
           `channel`/`uploader` (yt-dlp's channel-page `title` carries the
           tab, e.g. "Name - Videos", so it is only the last resort); for
           a playlist its `title`.
        2. `_media_title()`: the newest media's `playlist_title` (for a
           playlist source) or `channel`/`uploader` (for a channel).

        Tier 1 wins, except for a channel whose tier-2 value was retrieved
        after the cached row: a renamed channel's new videos carry the new
        name before `download_source_images` next refreshes the cache.

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

        Not cached here: `build_tvshow_nfo()` always wants a fresh read.
        The caching lives one layer up, in `resolve_show_title()`.
    '''
    cached_title = None
    if cached is not None:
        fields = ('title',) if source.is_playlist else (
            'channel', 'uploader', 'title',
        )
        for field in fields:
            cached_title = _clean_text(_value_dict(cached).get(field))
            if cached_title:
                break
    if cached_title and source.is_playlist:
        return cached_title
    media_title, media_retrieved = _media_title(source)
    if cached_title and media_title and media_retrieved is not None:
        if media_retrieved > cached.retrieved:
            return media_title
    return cached_title or media_title or None


def _display_title(source, data_title):
    '''
        The <title>/<showtitle> text: `data_title`, else `source.name` with
        emoji removed, else `source.name` as is (a name made only of emoji
        would otherwise leave an empty title).
    '''
    return data_title or _clean_text(source.name) or str(source.name).strip()


def _value_dict(cached):
    '''
        `cached.value` when it is a dict. `Metadata.value` is an
        unconstrained JSONField; any other shape carries no show data.
    '''
    value = cached.value
    return value if isinstance(value, dict) else {}


def _plot_from(cached):
    if cached is not None:
        return _clean_text(_value_dict(cached).get('description'))
    return ''


def _preserved_show_title(source):
    '''
        The `<title>` of a `tvshow.nfo` this writer leaves alone (see
        `_foreign_nfo_reason`), or `None`. Episode NFOs then name the same
        show as the file Plex/Kodi actually read, such as one the upstream
        `create-tvshow-nfo` command wrote with `source.name`. The title is
        only stripped, not emoji-cleaned, so it matches that file exactly.
    '''
    root, reason = _read_tvshow_nfo(source.directory_path / 'tvshow.nfo', source)
    if reason is None or root is None or root.tag != 'tvshow':
        return None
    return (root.findtext('title') or '').strip() or None


def resolve_show_title(source):
    '''
        Best available display title for an episode NFO's <showtitle>: the
        `<title>` of a `tvshow.nfo` this writer does not own (so episodes
        name the show that file names, emoji included), else
        `build_tvshow_nfo()`'s own `<title>` (`_resolve_show_title_from_data`,
        falling back to `source.name`), with emoji removed as that `<title>`
        has them removed. Either way it is the text the show file carries.

        Cached for `_SHOW_TITLE_CACHE_TTL_SECONDS` (60s), process-locally,
        keyed by `source.pk`. `Media.nfoxml` calls this once per episode,
        including from inside `rename_all_media_for_source`'s loop over
        every downloaded item of a source -- uncached, that is a few extra
        queries per item. TubeSync's tasks run via huey, potentially across
        more than one worker process, so this cache is not shared or
        invalidated across processes -- a stale title can survive up to the
        TTL in a worker that is not the one `write_tvshow_nfo()` last ran
        in, so an episode's <showtitle> can briefly lag the show's
        `tvshow.nfo`. That bounded staleness (one channel-name change, one
        worker, 60 seconds) is judged an acceptable trade for avoiding a
        shared cache's complexity. Within a process, `write_tvshow_nfo()`
        invalidates the entry whenever it recomputes, and a lookup that was
        already running at that point does not store its older result (see
        `_invalidate_show_title_cache`).

        An unsaved source (`pk` is `None`) bypasses the cache entirely: it
        has no stable key to cache under, and resolving it twice is rare
        (nothing calls this before a source is saved in normal operation).

        Never raises on a database or filesystem error: `django.db.Error`
        (which covers `InterfaceError` as well as `DatabaseError`) and
        `OSError` are caught, logged with a traceback, and the source's
        name is returned instead -- a lookup failure here must not fail
        the caller. `Media.nfoxml` is invoked from `write_nfo_file`
        (`sync/models/media__tasks.py`), which upstream only guards against
        `PermissionError`, and `download_media_file` calls it after the
        video has already downloaded. The queries run in a savepoint, so a
        database error does not leave an enclosing transaction (such as
        `rename_media`'s) unusable on PostgreSQL. A fallback produced by
        an error is deliberately not cached, so the next call retries the
        real lookup rather than pinning the fallback for the TTL.
    '''
    generation = None
    if source.pk is not None:
        with _show_title_lock:
            cached_entry = _show_title_cache.get(source.pk)
            generation = _show_title_generations.get(source.pk, 0)
        if cached_entry is not None:
            expires_at, title = cached_entry
            if time.monotonic() < expires_at:
                return title
    try:
        title = _preserved_show_title(source)
        if title is None:
            with transaction.atomic():
                cached = _cached_channel_metadata(source)
                data_title = _resolve_show_title_from_data(source, cached)
            title = _display_title(source, data_title)
    except (db.Error, OSError):
        log.exception(f'Failed to resolve show title for: {source}')
        return _display_title(source, None)
    if source.pk is not None:
        _store_show_title(source, generation, title)
    return title


def _with_checksum(content):
    '''
        `content` (serialized with `_CHECKSUM_PLACEHOLDER`) with the
        placeholder filled in: the sha256 of `content` itself.
    '''
    digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
    return content.replace(
        _CHECKSUM_PLACEHOLDER,
        f'{_CHECKSUM_PLACEHOLDER[:-1]}sha256:{digest}"',
        1,
    )


def _checksum_state(text):
    '''
        `None` when `text` carries no writer checksum, else whether the
        checksum still matches its content (`False` after a hand edit).
    '''
    match = _CHECKSUM_RE.search(text)
    if match is None:
        return None
    blanked = text[:match.start(2) - len('sha256:')] + text[match.end(2):]
    digest = hashlib.sha256(blanked.encode('utf-8')).hexdigest()
    return digest == match.group(2)


def build_tvshow_nfo(source):
    '''
        Returns a Kodi/Plex "tvshow.nfo" formatted (prettified) XML string
        for `source`. Looks the channel cache and latest media up once and
        derives <title>, <studio> and <plot> from that one snapshot.
        <title> is the same text `resolve_show_title()` gives episode NFOs
        while this writer owns the file.

        Carries two `<uniqueid>` elements: `type="youtube"` (the source's
        current, user-editable `key` -- the one Kodi/Plex/Jellyfin actually
        scrape against) and a second, non-`default` `type="tubesync"` (the
        source's immutable `uuid` primary key, with a `checksum` of the
        file). Extra `<uniqueid>` elements and attributes are ignored by
        those scrapers as long as none but the intended one is
        `default="true"`. The second one is what `_foreign_nfo_reason`
        recognises as this writer's own file, even after the user edits
        the source's `key`, and its checksum tells a hand-edited copy
        apart from an untouched one.
    '''
    cached = _cached_channel_metadata(source)
    studio = _resolve_show_title_from_data(source, cached)
    title = _display_title(source, studio)
    plot = _plot_from(cached)
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
    ownership_attrs['checksum'] = ''
    nfo.append(_nfo_element(
        nfo, 'uniqueid', str(source.uuid), attrs=ownership_attrs,
    ))
    if studio:
        nfo.append(_nfo_element(nfo, 'studio', studio))
    nfo[-1].tail = '\n'
    content = ElementTree.tostring(
        nfo, encoding='utf8', method='xml',
    ).decode('utf8')
    return _with_checksum(content)


def _foreign_nfo_reason(nfo_path, source):
    '''
        Why the file at `nfo_path` is not this writer's to replace, or None
        when it is: absent, empty, or a `<tvshow>` carrying this source's
        `<uniqueid type="tubesync">` (its immutable `uuid` primary key;
        only this writer emits that type) with a checksum that still
        matches the file. Otherwise:
          - another root, such as a video's own `<episodedetails>` from a
            `media_format` that renders a filename as `tvshow`; overwriting
            it would leave the two writers replacing each other's file;
          - this writer's file, edited since: its checksum no longer
            matches, or is missing or no longer in the exact form this
            writer emits (an editor reordered or requoted it); delete it
            to have it regenerated. Files from before the checksum existed
            are treated the same way (none were ever released);
          - any other `<tvshow>`, such as one written by hand or by the
            upstream `create-tvshow-nfo` command, which never overwrites.
            A `<uniqueid type="youtube">` with this source's key is not
            enough: any tool or person can write that one;
          - a non-empty file that does not parse as XML at all, such as a
            Kodi URL-only/combination NFO (a bare channel URL, no markup)
            or upstream `create-tvshow-nfo`'s own output when a channel
            name has a raw, unescaped "&" (the bug F5 left that command
            for) -- both are real, intentionally-placed files this writer
            must not silently clobber just because it cannot parse them.
            A zero-byte file is not treated this way: it carries no
            content to protect, so it is still replaceable, same as an
            absent one;
          - a symlink, dangling or not: writing would replace the link
            itself with a regular file.
    '''
    return _read_tvshow_nfo(nfo_path, source)[1]


def _read_tvshow_nfo(nfo_path, source):
    '''
        (parsed root or None, `_foreign_nfo_reason`'s reason), reading and
        parsing the file once for both callers. A live symlink's target is
        still parsed, so `resolve_show_title()` uses its `<title>`.
    '''
    if nfo_path.is_symlink():
        root = None
        if nfo_path.exists():
            root = _read_tvshow_nfo(nfo_path.resolve(), source)[0]
        return root, 'it is a symlink'
    if not nfo_path.exists():
        return None, None
    raw = nfo_path.read_bytes()
    if not raw:
        return None, None
    try:
        root = ElementTree.fromstring(raw)
    except (ElementTree.ParseError, LookupError, ValueError):
        # LookupError: an XML declaration naming an unknown encoding
        # (encoding="ANSI"); ValueError: a multi-byte one expat refuses
        # (encoding="UTF-32"). Both are files this writer cannot read.
        return None, 'it exists but could not be parsed as XML'
    if root.tag != 'tvshow':
        return root, (
            'it holds another NFO (does media_format render a video '
            'filename as "tvshow"?)'
        )
    tubesync_id = str(source.uuid)
    owned = any(
        uniqueid.get('type') == 'tubesync'
        and (uniqueid.text or '').strip() == tubesync_id
        for uniqueid in root.iter('uniqueid')
    )
    if not owned:
        return root, 'it is a tvshow.nfo this writer did not create'
    if _checksum_state(raw.decode('utf-8', errors='replace')) is not True:
        return root, (
            'it was edited after this writer created it; delete it to '
            'have it regenerated'
        )
    return root, None


def _tvshow_nfo_content_to_write(source, assume_directory_exists=False):
    '''
        Returns the tvshow.nfo text `write_tvshow_nfo()` would write for
        `source` right now, or `None` when nothing should be written:
        `write_nfo` is disabled, the source directory does not exist (see
        `assume_directory_exists`), the bytes on disk already match
        `build_tvshow_nfo()`'s output, or the file there is not this
        writer's to replace (`_foreign_nfo_reason`; a refusal is logged as
        a warning).

        The single place that builds the NFO: `tvshow_nfo_needs_write()`
        and `write_tvshow_nfo()` both call this instead of each calling
        `build_tvshow_nfo()` (and so re-deriving the show's title/plot
        from a fresh query) themselves -- Plex T4's backfill dry-run calls
        the former and apply calls the latter for the SAME source.

        `assume_directory_exists=True` skips the directory check: Plex T4's
        dry-run passes this when an overlay change means the corresponding
        apply run would create a still-missing source directory as a side
        effect of saving the source (`source_pre_save` ->
        `check_source_directory_exists`) before it ever gets to
        `write_tvshow_nfo()`, so the two modes predict the same outcome.
    '''
    if not source.write_nfo:
        return None
    directory = source.directory_path
    if not assume_directory_exists and not directory.is_dir():
        return None
    nfo_path = directory / 'tvshow.nfo'
    with transaction.atomic():
        content = build_tvshow_nfo(source)
    if nfo_path.exists() and nfo_path.read_bytes() == content.encode('utf-8'):
        return None
    reason = _foreign_nfo_reason(nfo_path, source)
    if reason:
        log.warning(f'Not writing tvshow.nfo for: {source}: {reason}')
        return None
    return content


def tvshow_nfo_needs_write(source, assume_directory_exists=False):
    '''
        True when `write_tvshow_nfo()` would write: `write_nfo` is enabled,
        the source directory exists, the bytes on disk differ from
        `build_tvshow_nfo()`, and the file there is this writer's to replace
        (`_foreign_nfo_reason`; a refusal is logged as a warning). Shared
        with Plex T4's backfill dry-run so its preview and the real write
        use one decision. See `_tvshow_nfo_content_to_write()` for the one
        place that decision (and the single `build_tvshow_nfo()` call it
        needs) actually lives, including `assume_directory_exists`.
    '''
    return _tvshow_nfo_content_to_write(source, assume_directory_exists) is not None


def write_tvshow_nfo(source, raise_errors=False):
    '''
        Writes `tvshow.nfo` for `source` when it needs writing (see
        `_tvshow_nfo_content_to_write()`/`tvshow_nfo_needs_write()`): only
        with `write_nfo` enabled, never into a missing source directory
        (creating it is `check_source_directory_exists`'s job), never over
        a file it did not create or that was edited since, and never when
        the bytes on disk already match -- so calling this from
        `index_source`, `download_source_images` and
        `download_media_metadata` every run does not churn the file (or
        its mtime). Never deletes anything. Returns True when it actually
        wrote the file, False otherwise.

        Best-effort by default: it runs at the tail of those tasks, after
        their real work has succeeded, so any error is logged with its
        traceback instead of failing, and so retrying, the calling task. `raise_errors=True` re-raises instead, for Plex
        T4's backfill command, which counts failures in its summary and
        exit status.

        Every call recomputes the show's data from scratch and drops this
        source's `resolve_show_title()` cache entry (see
        `_invalidate_show_title_cache`) -- this is that cache's one refresh
        point, so a subsequent `resolve_show_title()`/`Media.nfoxml` call in
        this process picks up the fresh value immediately rather than
        waiting out the TTL.

        Concurrent-write caveat (accepted limitation): two
        `download_media_metadata` tasks for the same source can finish
        concurrently and, if the resolved show title changes between one
        worker's `build_tvshow_nfo()` (by way of
        `_tvshow_nfo_content_to_write()`) and its `write_text_file()`, a
        stale snapshot can briefly replace a fresher `tvshow.nfo`. That
        lost update needs both tasks to overlap on the same source and the
        title to change between them -- in practice this is a one-time
        window when the first real channel name appears. `write_text_file()`
        is atomic (temp file plus replace), so the file is never corrupt.
        Every later `download_media_metadata`, `index_source` or
        `download_source_images` run rebuilds from current data and
        rewrites a stale file when the bytes differ. A per-source lock on
        the huey DB queue would serialise every metadata task of a source
        just to close that narrow window, so we accept this self-healing
        race instead.
    '''
    try:
        # Every call is a refresh point for this process's
        # resolve_show_title() cache (see the docstring above).
        _invalidate_show_title_cache(source)
        content = _tvshow_nfo_content_to_write(source)
        if content is None:
            return False
        log.info(f'Writing tvshow.nfo for: {source}')
        write_text_file(source.directory_path / 'tvshow.nfo', content)
        return True
    except Exception:
        # Deliberately broad: this runs at the tail of tasks whose real
        # work already succeeded, so no failure here (including one from
        # unexpected metadata shapes) may fail and retry them.
        if raise_errors:
            raise
        log.exception(f'Failed to write tvshow.nfo for: {source}')
        return False
