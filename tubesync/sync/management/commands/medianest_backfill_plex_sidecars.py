'''
    T4: applies the medianest_bridge T3 per-type source-defaults profile to
    one or more existing sources, then backfills the Plex TV-library
    sidecars (renamed video files, per-episode NFOs, thumbnails,
    tvshow.nfo) their already-downloaded media would have if they had been
    created with that profile from the start.

    This is an explicit, operator-run, one-shot maintenance command -- not
    a task hooked into any signal or schedule. It exists because T3's
    profile only ever applies going forward (build_source_form() is the
    create-time path only); a source created before T3 was configured, or
    before an operator changed MEDIANEST_BRIDGE_SOURCE_DEFAULTS, has no
    other way to catch up its existing downloaded media.

    Dry-run by default (nothing is written to the DB or the filesystem);
    --apply is required to actually change anything. Both modes share the
    exact same per-media/per-source decision logic (see _process_media(),
    _process_tvshow_and_images()) parameterized by `apply_changes`, so a
    dry-run's counts are computed the same way a real run's are, not by a
    separately-maintained approximation:
      - The T3 overlay is validated through the same SourceForm in both
        modes (_overlay_form()), bound to an in-memory copy of the source,
        and media.source points at that copy, so
        `media.filepath`/`media.nfoxml` etc. reflect the WOULD-BE values
        without saving anything.
      - In apply mode, only the overlay fields that change are saved, onto
        a freshly read row (_save_overlay()), before the per-media loop
        runs, so the same property reads reflect the real new values and
        a concurrent edit to any other field is kept.
      - Dry-run turns SHRINK_OLD_MEDIA_METADATA off, since reading
        metadata with it on writes the shrunk metadata back.
      - Sidecar paths come from the profile filename in both modes
        (_sidecar_path()), and tvshow.nfo uses the same
        tvshow_nfo_needs_write() decision the real write does -- including,
        in dry-run, assuming a still-missing source directory would exist
        by then, when an overlay change means apply's save would create it
        first (source_pre_save's own check_source_directory_exists call).

    Failure handling: each media is renamed without a wrapping
    transaction (rename_files() saves media_file as soon as the video
    moves) and its NFO/thumbnail are written afterwards, so no later
    failure can roll back the media_file update of a file that already
    moved. A missing current file, an occupied target for the video (on
    disk or claimed earlier in this run), an occupied destination for any
    sidecar rename_files() would move, OR an already-occupied target-side
    .nfo that no move of this media's own would bring (this command's own
    NFO write would otherwise silently clobber it right after the video
    moves), is an error (nothing moves and the media's sidecars are
    skipped). So is a path rename_files()'s `{key}` sweep would take that
    belongs to another media, or is a directory; the sweep's other moves
    are listed. Adopting an earlier half-finished move that left stray
    same-key sidecars behind, or whose target is a symlink or resolves
    outside DOWNLOAD_ROOT, is also an error (nothing is adopted, moved or
    deleted). A symlink at any destination counts as occupied, even a
    dangling one, and so does a path an earlier media of the run already
    claims (projected in a dry-run). A media already at its target gets
    the rename's path checks before its NFO and thumbnail are written.
    Media that finish downloading during --apply are
    processed before it ends, and after an overlay that can change the
    rendered path (_PATH_FIELDS) media still busy downloading are counted
    as in flight.
    Every per-media failure is both logged and printed to stdout, so the
    final "see the output above" is accurate. Each source is isolated
    from the others, and the command exits non-zero when anything errored
    or was skipped as locked or in flight -- re-run it once the cause is
    fixed.
    --apply refuses to run unless the effective user owns DOWNLOAD_ROOT,
    so everything it creates stays writable by TubeSync
    (`docker exec -u app ...`).

    Never deletes any file (grep this module: no unlink/rmtree/os.remove
    call). `Media.rename_files()`'s own empty-directory cleanup (an
    `rmdir()` on now-empty leftover directories after a move) is existing
    upstream behavior, not something this command adds or could disable;
    it never removes a non-empty directory or a file.

    Saving the source (via SourceForm, same as a real edit) fires
    TubeSync's own Source post_save signal
    (sync/signals.py::source_post_save), which unconditionally schedules
    save_all_media_for_source -- and that task, if a huey consumer is
    running and later processes it, always schedules
    rename_all_media_for_source in turn. Both are asynchronous
    (huey-enqueued, not executed by this command). Unlike this command,
    rename_all_media_for_source calls upstream Media.rename_files()
    directly with none of this command's own refusal checks (occupied
    target, occupied sidecar destination, claimed prefix, adoption
    leftovers), and Path.replace() silently overwrites a same-stem
    sidecar at the destination. rename_all_media_for_source only skips a
    source when BOTH settings.RENAME_ALL_SOURCES is False AND
    source.directory is not listed in settings.RENAME_SOURCES --
    RENAME_ALL_SOURCES defaults to True (tubesync/settings.py,
    local_settings.py.container), so on a typical deployment the cascade
    WILL fire a few minutes after this command saves a source, and would
    silently clobber exactly the media this command itself refused to
    touch.

    To prevent that, --apply runs the exact same per-media decision logic
    as a dry-run (see _count_refused_media()) BEFORE saving any source
    whose overlay would actually change a field: when the cascade is
    enabled for that source (_cascade_enabled_for(), mirroring
    rename_all_media_for_source's own gate) and any media would be
    refused, the source is not saved at all -- nothing for that source is
    changed, one error is counted, and the operator is told to resolve
    the conflicts or disable the cascade (TUBESYNC_RENAME_ALL_SOURCES and
    TUBESYNC_RENAME_SOURCES) before re-running. The same happens, counted
    as in flight instead, when the overlay can change the rendered path
    and any of the source's media is downloading right now: that
    download finishes under the old name and the queued cascade would
    rename it later, unchecked. Dry-run runs the same preflight and stops
    the source the same way, so its summary matches --apply's. This
    check runs just before the save, so a download that starts between
    the two is still possible; the post-save in-flight count reports it.
    Where the gate lets a save through
    (or the cascade is disabled/not enabled for that source),
    Media.rename_files() is itself idempotent -- calling it again after
    this command already renamed everything simply returns immediately
    (`old_video_path == new_video_path`) -- so the eventual cascade run is
    a harmless no-op for every media this command actually completed. In
    tests, huey enqueue-without-a-consumer never actually executes
    anything (see the wider test suite's existing reliance on this same
    fact for signals.py's own enqueue calls) -- these tests never see the
    cascade itself run; the gate is exercised by asserting the source is
    (or is not) saved.
'''
import copy
import os
import stat
from contextlib import contextmanager, nullcontext
from pathlib import Path
from uuid import UUID
from xml.etree import ElementTree

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.forms.models import model_to_dict
from django.utils.translation import gettext_lazy as _
from django_huey import lock_task as huey_lock_task
from huey.exceptions import TaskLockedException

from common.logger import log
from common.models import TaskHistory
from common.utils import directory_and_stem, glob_quote
from medianest_bridge.config import load_validated_source_defaults
from medianest_bridge.source_forms import (
    LIST_SHAPED_FIELDS, coerce_list_shaped_fields, extract_form_errors,
    run_edit_source_checks,
)
from sync.choices import TaskQueue, Val, YouTube_SourceType
from sync.forms import SourceForm
from sync.models import Media, Source
from sync.models._migrations import media_file_storage
from sync.tasks import download_source_images
from sync.tvshow_nfo import tvshow_nfo_needs_write, write_tvshow_nfo
from sync.utils import write_text_file

# The prefix MediaNest's acquisition-source-write service gives every
# source it creates (deriveInjectiveName() -> `acq-src-${key}`, truncated
# to 100 characters) for BOTH `name` and `directory` -- see the T4 plan
# brief's F1. --all-bridge-sources intentionally checks both fields, not
# just one, to stay conservative about what counts as "bridge-created".
BRIDGE_SOURCE_PREFIX = 'acq-src-'

# T3's per-type profile is keyed by the bridge's own contract source
# types ('channel'/'playlist' -- medianest_bridge/source_forms.py), which
# map onto exactly two of TubeSync's three real source_type values.
# CHANNEL ('c', handle-based) has no contract equivalent -- the bridge
# never creates one (see source_forms.py's own module docstring, point 1)
# -- so a source of that type has no T3 profile to apply and is skipped
# (counted as an error) rather than guessed at.
_TUBESYNC_TO_CONTRACT_SOURCE_TYPE = {
    Val(YouTube_SourceType.CHANNEL_ID): 'channel',
    Val(YouTube_SourceType.PLAYLIST): 'playlist',
}

_SUMMARY_FIELDS = (
    'sources', 'media_seen', 'renamed', 'adopted', 'already_in_place',
    'key_matched_moves', 'nfo_written', 'nfo_unchanged', 'thumbs_copied',
    'tvshow_written', 'images_enqueued', 'locked', 'in_flight', 'errors',
)

# Overlay fields that can change a media's rendered path: media_format
# itself, and the format-selection fields behind Media.format_dict's
# {format}/{resolution}/{height}/{width}/{vcodec}/{acodec}/{fps}/{hdr}
# keys (get_format_str(), get_display_format(), sync/matching.py) and
# Source.extension's {ext}. name, directory and source_type feed it too,
# but a T3 overlay may not set them (SOURCE_DEFAULTS_FORBIDDEN_FIELDS).
_PATH_FIELDS = frozenset((
    'media_format', 'source_resolution', 'source_vcodec', 'source_acodec',
    'prefer_60fps', 'prefer_hdr', 'fallback',
))


# Every file sync/tasks.py::download_source_images writes into the source
# directory, each with a plain open(..., 'wb') that follows a symlink.
_SOURCE_IMAGE_NAMES = (
    'thumbnail.jpg', 'banner.jpg', 'background.jpg', 'poster.jpg',
    'season-poster.jpg',
)


def _occupied(path):
    '''
        True when anything is at `path`, including a dangling symlink,
        which Path.exists() reports as absent but Path.rename()/replace()
        would silently replace.
    '''
    return path.exists() or path.is_symlink()


@contextmanager
def _shrink_old_metadata_off():
    '''
        Reading metadata with TUBESYNC_SHRINK_OLD (SHRINK_OLD_MEDIA_METADATA)
        on rewrites it in the database (Media.loaded_metadata ->
        reduce_data -> ingest_metadata); a dry-run must not. This changes
        the setting for the whole process, which is this command's own
        `manage.py` process -- it is an operator-run, one-shot command.
    '''
    missing = object()
    previous = getattr(settings, 'SHRINK_OLD_MEDIA_METADATA', missing)
    settings.SHRINK_OLD_MEDIA_METADATA = False
    try:
        yield
    finally:
        if previous is missing:
            del settings.SHRINK_OLD_MEDIA_METADATA
        else:
            settings.SHRINK_OLD_MEDIA_METADATA = previous


class Command(BaseCommand):

    help = (
        'Applies the medianest_bridge T3 source-defaults profile to '
        'existing sources and backfills Plex TV-library sidecars '
        '(renamed files, episode/tvshow NFOs, thumbnails) for their '
        'already-downloaded media. Dry-run by default; pass --apply to '
        'actually change anything.'
    )

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument(
            '--source', dest='sources', action='append', metavar='UUID',
            help='A source UUID to process. Repeatable.',
        )
        group.add_argument(
            '--all-bridge-sources', action='store_true', default=False,
            help=(
                'Process every source whose directory AND name both '
                f'start with "{BRIDGE_SOURCE_PREFIX}" (bridge-created).'
            ),
        )
        parser.add_argument(
            '--apply', action='store_true', default=False,
            help='Actually write changes. Without this flag, only the '
                 'plan is printed; nothing is changed on disk or in the DB.',
        )

    def handle(self, *args, **options):
        if options['apply']:
            return self._run(options, apply_changes=True)
        with _shrink_old_metadata_off():
            return self._run(options, apply_changes=False)

    def _run(self, options, apply_changes):

        defaults_by_type, config_errors = load_validated_source_defaults()
        if config_errors:
            raise CommandError(
                'MEDIANEST_BRIDGE_SOURCE_DEFAULTS is invalid; refusing to '
                'change anything:\n' +
                '\n'.join(f'  - {message}' for message in config_errors)
            )
        # rename_files() records every new path relative to the storage
        # location after resolving it; a location reached through a
        # symlink makes that fail after the video has moved, before the
        # row or its sidecars follow. Refuse the run, in both modes.
        storage_root = Path(media_file_storage.location)
        if storage_root.resolve() != storage_root.absolute():
            raise CommandError(
                f'the media storage location {storage_root} goes through a '
                f'symlink (it resolves to {storage_root.resolve()}); '
                'rename_files() cannot record paths under it. Point '
                'DOWNLOAD_ROOT at the real directory; nothing was changed.'
            )
        if apply_changes:
            self._check_running_as_download_owner()

        sources = self._resolve_sources(options)
        if not sources:
            self.stdout.write('No matching sources found.')
            return

        # _stray_snapshot()'s per-source directory-listing cache, built
        # lazily (see its own docstring); one Command instance per
        # call_command() invocation, so this never leaks between runs.
        self._stray_snapshot_cache = {}
        summary = dict.fromkeys(_SUMMARY_FIELDS, 0)
        for source in sources:
            summary['sources'] += 1
            try:
                self._process_source(
                    source, defaults_by_type, apply_changes, summary,
                )
            except Exception:
                # One source's failure (a DB error saving it, a broker
                # error enqueueing its images) must not abort the rest
                # of an --all-bridge-sources run.
                summary['errors'] += 1
                log.exception(
                    f'medianest_backfill_plex_sidecars: error processing {source}'
                )
                self.stdout.write(self.style.ERROR(
                    '  FAILED: unexpected error, see the log',
                ))

        self._print_summary(summary, apply_changes)
        if summary['errors'] or summary['locked'] or summary['in_flight']:
            raise CommandError(
                f'{summary["errors"]} error(s), {summary["locked"]} locked '
                f'and {summary["in_flight"]} in-flight media; see the output '
                'above. Nothing was deleted; re-run once the cause is fixed '
                '(locked and in-flight media are picked up by the next run).'
            )

    def _check_running_as_download_owner(self):
        '''
            Files and "Season YYYY/" directories this command creates are
            owned by whoever runs it. Run as root (a plain `docker exec`),
            they would be root-owned and the app user's huey workers could
            no longer download or rename into them. Refuse --apply unless
            the effective user owns DOWNLOAD_ROOT, like the app user does.
        '''
        download_root = Path(settings.DOWNLOAD_ROOT)
        owner = download_root.stat().st_uid
        if os.geteuid() != owner:
            raise CommandError(
                f'--apply must run as the user that owns {download_root} '
                f'(uid {owner}), not uid {os.geteuid()}, so new files and '
                'directories stay writable by TubeSync. In the container: '
                'docker exec -u app <container> python3 /app/manage.py '
                'medianest_backfill_plex_sidecars ...'
            )

    def _resolve_sources(self, options):
        if options['all_bridge_sources']:
            return list(
                Source.objects.filter(
                    directory__startswith=BRIDGE_SOURCE_PREFIX,
                    name__startswith=BRIDGE_SOURCE_PREFIX,
                ).order_by('name')
            )
        sources = []
        for raw in options['sources']:
            try:
                source_uuid = UUID(str(raw))
            except ValueError as exc:
                raise CommandError(f'Not a valid source UUID: {raw!r}') from exc
            try:
                sources.append(Source.objects.get(pk=source_uuid))
            except Source.DoesNotExist as exc:
                raise CommandError(f'No such source: {raw}') from exc
        return sources

    def _process_source(self, source, defaults_by_type, apply_changes, summary):
        mode = 'apply' if apply_changes else 'dry-run'
        self.stdout.write(f'[{mode}] {source.name} ({source.uuid})')

        contract_type = _TUBESYNC_TO_CONTRACT_SOURCE_TYPE.get(source.source_type)
        if contract_type is None:
            summary['errors'] += 1
            message = (
                f'source_type {source.source_type!r} has no T3 profile '
                '(only channel-by-ID and playlist sources do)'
            )
            log.error(f'medianest_backfill_plex_sidecars: {source}: {message}')
            self.stdout.write(self.style.ERROR(f'  SKIPPED: {message}'))
            return

        # A source directory path taken by something that is not a
        # directory (a regular file, or a symlink that does not resolve to
        # one) cannot be used in either mode: a dry-run would assume a save
        # creates the directory, while apply's save fails in
        # check_source_directory_exists (os.makedirs raises
        # FileExistsError). Refuse it the same way in both.
        directory = Path(source.directory_path)
        if _occupied(directory) and not directory.is_dir():
            summary['errors'] += 1
            message = (
                f'source directory {directory} exists but is not a '
                'directory; move it aside and re-run'
            )
            log.error(f'medianest_backfill_plex_sidecars: {source}: {message}')
            self.stdout.write(self.style.ERROR(f'  SKIPPED: {message}'))
            return

        # One precondition instead of a check at every path the backfill
        # or rename_files() touches: the source's tree must hold only
        # regular files and real directories. rename_files() resolves
        # symlinks while this command's checks compare paths lexically,
        # skips dangling links, and moves FIFOs and sockets as if they were
        # sidecars; NFO reads block on a FIFO. Refused in both modes before
        # anything is saved or moved. The per-path checks further down stay
        # as defense in depth.
        special = self._special_tree_entries(directory)
        if special:
            summary['errors'] += 1
            shown = ', '.join(str(path) for path in special[:10])
            more = f' and {len(special) - 10} more' if len(special) > 10 else ''
            message = (
                'the source directory holds symlinks or special files '
                f'({shown}{more}); the backfill only handles regular files '
                'and real directories. Replace or remove them and re-run'
            )
            log.error(f'medianest_backfill_plex_sidecars: {source}: {message}')
            self.stdout.write(self.style.ERROR(f'  SKIPPED: {message}'))
            return
        # The source directory's own path must be canonical too: a symlink
        # on it (an ancestor below the storage location; the location
        # itself is checked per run) would let the save's
        # check_source_directory_exists create the directory wherever the
        # link points, outside DOWNLOAD_ROOT included, before any later
        # check runs.
        if directory.resolve() != directory.absolute():
            summary['errors'] += 1
            message = (
                f'the source directory {directory} goes through a symlink '
                f'(it resolves to {directory.resolve()}); point the source '
                'at the real directory and re-run'
            )
            log.error(f'medianest_backfill_plex_sidecars: {source}: {message}')
            self.stdout.write(self.style.ERROR(f'  SKIPPED: {message}'))
            return

        overlay = defaults_by_type.get(contract_type, {})
        working_source = source
        images_already_queued = False
        overlay_changed = False
        changes = {}
        if overlay:
            original = {field: getattr(source, field, None) for field in overlay}
            # Bound to a copy in both modes: ModelForm validation writes
            # every form field into its instance, and apply saves only the
            # changed overlay fields onto a fresh row (_save_overlay()).
            form = self._overlay_form(copy.copy(source), overlay)
            if not form.is_valid():
                summary['errors'] += 1
                messages = '; '.join(extract_form_errors(form))
                log.error(
                    f'medianest_backfill_plex_sidecars: {source}: T3 profile '
                    f'failed validation: {messages}'
                )
                self.stdout.write(self.style.ERROR(f'  SKIPPED: {messages}'))
                return
            changes = self._overlay_changes(original, form.cleaned_data, overlay)
            self._describe_overlay_diff(original, changes)
            overlay_changed = bool(changes)
            # Turning copy_channel_images on makes source_pre_save enqueue
            # download_source_images itself; suppress this command's own
            # direct enqueue below but still count it (both modes).
            images_already_queued = bool(changes.get('copy_channel_images'))
            working_source = form.instance

        # Image-queue gate: saving an overlay that turns
        # copy_channel_images on (_save_overlay() below) fires
        # source_pre_save, which unconditionally queues
        # download_source_images for this same save -- before
        # _process_tvshow_and_images()'s own `images_already_queued`
        # handling ever runs. That task opens
        # every file it writes (thumbnail.jpg, banner.jpg, background.jpg,
        # poster.jpg, season-poster.jpg) with a plain open(..., 'wb'),
        # which follows a symlink (even a dangling one) and writes through
        # it, and a source directory resolving outside DOWNLOAD_ROOT is
        # unsafe too. Refuse the source in both modes before anything is
        # saved, rather than letting the save queue the job first.
        if images_already_queued:
            unsafe = self._unsafe_image_queue_problems(working_source)
            if unsafe:
                summary['errors'] += 1
                message = (
                    f'{source}: turning copy_channel_images on queues '
                    "TubeSync's own image download, which would write "
                    'through ' + '; '.join(unsafe) + '; replace the '
                    'link(s) with regular files (or move the directory '
                    'inside DOWNLOAD_ROOT) and re-run'
                )
                if apply_changes:
                    log.error(
                        f'medianest_backfill_plex_sidecars: {message}'
                    )
                    self.stdout.write(self.style.ERROR(f'  SKIPPED: {message}'))
                else:
                    self.stdout.write(self.style.WARNING(
                        '  NOTE: --apply would skip this source without '
                        f'saving it: {message}'
                    ))
                return

        downloaded = list(
            Media.objects.filter(source=source, downloaded=True)
            .select_related('source').order_by('key')
        )
        media_files = {
            Path(media.media_file.path) for media in downloaded if media.media_file
        }
        # Every ownership check compares recorded paths lexically, while
        # rename_files() resolves them. A row recorded through a symlinked
        # directory below the storage location (an alias of another
        # directory there) would make those comparisons miss: another
        # row's video, seen through the alias, would look like an
        # unclaimed sidecar and be moved. Refuse the whole source, in both
        # modes, before anything is saved or moved.
        storage_root = Path(media_file_storage.location)
        aliases = sorted({
            str(linked) for linked in (
                self._symlinked_ancestor(path.parent, storage_root)
                for path in media_files
            ) if linked is not None
        })
        if aliases:
            summary['errors'] += 1
            message = (
                'downloaded media are recorded through symlinked '
                'directories (' + ', '.join(aliases) + '); '
                'rename_files() resolves paths through them, so ownership '
                'checks could not be trusted. Record the real paths and re-run'
            )
            if apply_changes:
                log.error(f'medianest_backfill_plex_sidecars: {source}: {message}')
                self.stdout.write(self.style.ERROR(f'  SKIPPED: {message}'))
            else:
                self.stdout.write(self.style.WARNING(
                    '  NOTE: --apply would skip this source without saving '
                    f'it: {message}'
                ))
            return
        # The ownership checks only know this source's rows, while
        # rename_files() globs the video's directory for old-stem sidecars
        # (a recorded path can already sit in a nested directory) and, with
        # {key}, sweeps the whole tree. Another source sharing this
        # directory (through an alias), or nested inside it, could have a
        # file moved and its row left pointing at nothing, regardless of
        # media_format. Refuse such a source, in both modes, before
        # anything is saved or moved.
        nested = self._nested_source_directories(source, working_source)
        if nested:
            summary['errors'] += 1
            message = (
                "other sources' directories are this directory, inside it "
                'or contain it (' + ', '.join(nested) + '); '
                'rename_files() could move their files. Separate them and '
                're-run'
            )
            if apply_changes:
                log.error(f'medianest_backfill_plex_sidecars: {source}: {message}')
                self.stdout.write(self.style.ERROR(f'  SKIPPED: {message}'))
            else:
                self.stdout.write(self.style.WARNING(
                    '  NOTE: --apply would skip this source without saving '
                    f'it: {message}'
                ))
            return
        # Where every video was before this run moved anything; a dry-run
        # leaves other media's sidecars there (see _claimed_by_other_media).
        self._original_media_files = frozenset(media_files)
        # Sidecar and key-match destinations of this source's renames so
        # far; a dry-run only projects them (_reserved_paths()).
        self._projected_destinations = set()
        # Both modes read media.filepath/media.source.* against the
        # would-be values from here on: `working_source` is the validated
        # copy (or `source` itself when there is no overlay).
        for media in downloaded:
            media.source = working_source

        # Paths each media has or will have (its video, current and
        # target, and the episode NFO and thumbnail this command or
        # rename_files() generates) must not collide with the show's
        # tvshow.nfo, with a channel-image file name while channel images
        # are on (download_source_images overwrites those with a plain
        # open()), with the video's own target, or with a sidecar another
        # media generates. Refuse such a source, in both modes, before
        # anything is saved or moved; media that finish downloading during
        # the run are checked against the same claims (see
        # _process_late_downloads()).
        self._claimed_paths = {}
        self._claimed_generated = set()
        problems = []
        for media in downloaded:
            problems.extend(
                f'{media}: {problem}' for problem in
                self._reserved_path_problems(
                    media, working_source, self._claimed_paths,
                )
            )
        if problems:
            summary['errors'] += 1
            shown = '; '.join(problems[:10])
            more = f' and {len(problems) - 10} more' if len(problems) > 10 else ''
            message = (
                f'reserved paths collide ({shown}{more}); change the '
                'media_format or the affected options and re-run'
            )
            if apply_changes:
                log.error(f'medianest_backfill_plex_sidecars: {source}: {message}')
                self.stdout.write(self.style.ERROR(f'  SKIPPED: {message}'))
            else:
                self.stdout.write(self.style.WARNING(
                    '  NOTE: --apply would skip this source without saving '
                    f'it: {message}'
                ))
            return

        # Rename-cascade gate: saving a source whose overlay actually
        # changes a field fires source_post_save's
        # save_all_media_for_source -> rename_all_media_for_source cascade
        # (see the module docstring). When that cascade is enabled for
        # this source, run the exact same per-media decision logic a
        # dry-run would (on a scratch copy, touching nothing) BEFORE
        # deciding whether --apply may save the source at all. A download
        # already running when a path-changing overlay is saved finishes
        # under the old name, and the queued cascade would then rename it
        # unchecked, so in-flight media refuse the save too. Both modes
        # stop the source here, so a dry-run's summary matches --apply's.
        path_changing = bool(_PATH_FIELDS.intersection(changes))
        if overlay_changed and self._cascade_enabled_for(source):
            gate = None
            refused = self._count_refused_media(downloaded, media_files)
            if refused:
                gate = ('errors', 1, self._cascade_gate_message(refused))
            elif path_changing and (busy := self._in_flight_media(source)):
                for media in busy:
                    self._report_in_flight(media)
                gate = ('in_flight', len(busy), self._in_flight_gate_message(busy))
            if gate is not None:
                field, count, message = gate
                summary[field] += count
                if apply_changes:
                    log.error(
                        f'medianest_backfill_plex_sidecars: {source}: {message}'
                    )
                    self.stdout.write(self.style.ERROR(f'  SKIPPED: {message}'))
                else:
                    self.stdout.write(self.style.WARNING(
                        '  NOTE: --apply would skip this source without '
                        f'saving it: {message}'
                    ))
                return
        if apply_changes and changes:
            # Saving when no overlay field changes would still fire
            # source_post_save and its save_all_media_for_source cascade
            # on every re-run.
            working_source = self._save_overlay(source, changes)
            for media in downloaded:
                media.source = working_source

        for media in downloaded:
            summary['media_seen'] += 1
            self._process_media(media, apply_changes, summary, media_files)
        if apply_changes:
            self._process_late_downloads(
                source, working_source, downloaded, summary, media_files,
            )
            if path_changing:
                self._count_in_flight(source, summary)

        self._process_tvshow_and_images(
            working_source, apply_changes, summary, images_already_queued,
            overlay_changed,
        )

    def _save_overlay(self, source, changes):
        '''
            Saves only the changed overlay fields onto a freshly read row,
            so a concurrent edit to any other field since this run read the
            source (including target_schedule, which the scheduler
            rewrites) is kept, and unchanged fields are not re-normalized
            by the form. A save with `update_fields` still fires
            source_pre_save/source_post_save, and so the rename cascade the
            gate above accounts for. Returns the saved row.
        '''
        fresh = Source.objects.get(pk=source.pk)
        for field, value in changes.items():
            Source._meta.get_field(field).save_form_data(fresh, value)
        fresh.save(update_fields=sorted(changes))
        return fresh

    def _process_late_downloads(
        self, source, working_source, downloaded, summary, media_files,
    ):
        '''
            Processes media that finished downloading after this run read
            the source's downloaded media, so a download that was already
            running lands in the profile layout by the end of the run
            instead of staying under the old name. Repeats until no new
            ones appear (bounded, in case downloads keep finishing).
        '''
        seen = {media.pk for media in downloaded}
        for _attempt in range(3):
            late = list(
                Media.objects.filter(source=source, downloaded=True)
                .exclude(pk__in=seen).order_by('key')
            )
            if not late:
                return
            for media in late:
                seen.add(media.pk)
                media.source = working_source
                if media.media_file:
                    media_files.add(Path(media.media_file.path))
            for media in late:
                summary['media_seen'] += 1
                self.stdout.write(f'  finished downloading during this run: {media}')
                problems = self._reserved_path_problems(
                    media, working_source, self._claimed_paths,
                )
                if problems:
                    self._media_error(
                        summary, f'{media}: not processed (reserved paths '
                        'collide: ' + '; '.join(problems) + ')',
                    )
                    continue
                self._process_media(media, True, summary, media_files)

    def _in_flight_media(self, source):
        '''
            Media of `source` that could be downloading right now: not
            downloaded yet and holding their `media:<uuid>` lock
            (download_media_file holds it for the whole download). Skipped
            rows count too: marking an item skipped does not stop a
            download that is already running.
        '''
        candidates = Media.objects.filter(
            source=source, downloaded=False,
        ).only('pk', 'uuid', 'key', 'title')
        return [
            media for media in candidates
            if huey_lock_task(
                f'media:{media.uuid}', queue=Val(TaskQueue.DB),
            ).is_locked()
        ]

    def _report_in_flight(self, media):
        self.stdout.write(self.style.WARNING(
            f'  IN FLIGHT: {media} is busy (likely downloading) and '
            'may finish under the old name; re-run once it is done.'
        ))

    def _count_in_flight(self, source, summary):
        '''
            Counts the in-flight media after a path-changing overlay was
            saved (one that started after the gate's own check, or any
            when the cascade is off): a download that started before the
            save finishes into the old layout, so the run exits non-zero
            and asks for a re-run once those are done.
        '''
        for media in self._in_flight_media(source):
            summary['in_flight'] += 1
            self._report_in_flight(media)

    def _in_flight_gate_message(self, busy):
        return (
            f'{len(busy)} media item(s) are downloading right now and would '
            'finish under the old name; saving this source would fire '
            "TubeSync's own rename_all_media_for_source cascade, which "
            "would later rename them without this command's own refusal "
            'checks. Not saving this source. Wait for the downloads to '
            'finish and re-run, or set TUBESYNC_RENAME_ALL_SOURCES=false '
            "(and remove this source's directory from "
            'TUBESYNC_RENAME_SOURCES) first.'
        )

    def _cascade_enabled_for(self, source):
        '''
            True when saving `source` would leave the unguarded upstream
            rename_all_media_for_source cascade able to do something a
            few minutes later -- mirrors sync/tasks.py's own
            rename_all_media_for_source gate (RENAME_SOURCES /
            RENAME_ALL_SOURCES) exactly, so this predicts precisely when
            that task would touch this source's media.
        '''
        rename_sources = getattr(settings, 'RENAME_SOURCES', None) or ()
        return bool(
            (source.directory and source.directory in rename_sources) or
            getattr(settings, 'RENAME_ALL_SOURCES', False)
        )

    def _count_refused_media(self, downloaded, media_files):
        '''
            How many of `downloaded` would be refused by _rename_media()
            right now, using the exact same dry-run (apply_changes=False)
            decision logic a real dry-run uses -- the preflight the
            rename-cascade gate runs before letting --apply save a source
            whose overlay changed a field. Callers must already have set
            each media's `.source` to the would-be source, same as the
            real dry-run loop does. Works on COPIES of `media_files` and
            the projected destinations and a scratch summary dict, so this
            preflight cannot itself affect the real run that follows it
            when nothing is refused.
        '''
        scratch_summary = dict.fromkeys(_SUMMARY_FIELDS, 0)
        scratch_media_files = set(media_files)
        projected = self._projected_destinations
        self._projected_destinations = set(projected)
        refused = 0
        try:
            for media in downloaded:
                try:
                    renamed_ok = self._rename_media(
                        media, scratch_summary, False, scratch_media_files,
                    )
                except Exception:
                    renamed_ok = False
                    log.exception(
                        'medianest_backfill_plex_sidecars: cascade-gate '
                        f'preflight error for {media}'
                    )
                if not renamed_ok:
                    refused += 1
        finally:
            self._projected_destinations = projected
        return refused

    def _cascade_gate_message(self, refused):
        return (
            f'{refused} already-downloaded media item(s) would be refused '
            'by the rename (re-run without --apply to see which ones and '
            "why); saving this source would fire TubeSync's own "
            'rename_all_media_for_source cascade a few minutes later, '
            "which has none of this command's own refusal checks and "
            'would silently overwrite those same-stem sidecars. Not '
            'saving this source. Resolve the conflicts and re-run, or set '
            'TUBESYNC_RENAME_ALL_SOURCES=false (and remove this '
            "source's directory from TUBESYNC_RENAME_SOURCES) first."
        )

    def _overlay_form(self, source, overlay):
        '''
            A SourceForm bound to `source` with `overlay` applied onto the
            source's CURRENT field values (not blank Source() defaults --
            unlike medianest_bridge.source_forms.build_source_form(), which
            only ever builds a brand-new POST /sources create), so an
            existing operator's own customizations to fields the overlay
            doesn't touch (filter rules, resolution, days_to_keep, etc.)
            are preserved. Validated through SourceForm plus
            run_edit_source_checks(), which reproduces (does not share)
            sync/views/sources.py::EditSourceMixin.form_valid()'s two extra
            checks -- keep the two in step if that view changes. Binding
            the form updates `source` in memory (ModelForm validation
            does), so callers pass a copy.
        '''
        data = model_to_dict(source, fields=list(SourceForm.base_fields.keys()))
        for field, value in data.items():
            # A saved CommaSepChoiceField (sponsorblock_categories) loads as
            # a CommaSepChoice tuple, not the list its form field expects.
            if hasattr(value, 'selected_choices'):
                data[field] = list(value.selected_choices)
        data.update(overlay)
        coerce_list_shaped_fields(data)
        form = SourceForm(data=data, instance=source)
        if form.is_valid():
            run_edit_source_checks(form)
        return form

    def _overlay_changes(self, original, cleaned, overlay):
        '''
            The overlay fields whose validated value differs from the
            source's `original` one. Comparing the form's cleaned value, not
            the raw JSON, keeps a re-run a no-op when the form normalizes it
            (`"3600"` to 3600, a stripped media_format).
        '''
        changes = {}
        for field in overlay:
            value = cleaned.get(field, overlay[field])
            before = self._comparable(field, original[field])
            if before != self._comparable(field, value):
                changes[field] = value
        return changes

    def _comparable(self, field, value):
        '''
            A list-shaped field (sponsorblock_categories) is saved as a
            CommaSepChoice but configured as a string or list, so both
            sides compare as a sorted list of individual choices.
        '''
        if field not in LIST_SHAPED_FIELDS:
            return value
        if hasattr(value, 'selected_choices'):
            value = list(value.selected_choices)
        value = coerce_list_shaped_fields({field: value})[field]
        return sorted(
            choice for item in value for choice in str(item).split(',') if choice
        )

    def _describe_overlay_diff(self, original, changes):
        if not changes:
            self.stdout.write('  T3 profile already applied (no field changes).')
            return
        self.stdout.write('  T3 profile field changes:')
        for field in sorted(changes):
            self.stdout.write(
                f'    {field}: {original[field]!r} -> {changes[field]!r}'
            )

    def _process_media(self, media, apply_changes, summary, media_files):
        try:
            if apply_changes:
                locks = (
                    huey_lock_task(
                        f'index_media:{media.uuid}', queue=Val(TaskQueue.FS),
                    ),
                    huey_lock_task(
                        f'media:{media.uuid}', queue=Val(TaskQueue.DB),
                    ),
                )
            else:
                locks = (nullcontext(), nullcontext())
            with locks[0], locks[1]:
                # No transaction: rename_files() saves media_file right
                # after it moves the video and then keeps going (sidecar
                # moves, its own NFO rewrite). Rolling that save back on a
                # later failure would leave the database pointing at a
                # file that has already moved.
                outcome = self._rename_media(
                    media, summary, apply_changes, media_files,
                )
                if outcome:
                    self._handle_episode_nfo(
                        media, summary, apply_changes,
                        renamed=outcome == 'renamed',
                    )
                    self._handle_thumbnail(media, summary, apply_changes)
        except TaskLockedException as exc:
            summary['locked'] += 1
            log.warning(
                f'medianest_backfill_plex_sidecars: {media} is locked by '
                f'another task ({exc}); skipping it this run.'
            )
            self.stdout.write(self.style.WARNING(
                f'  LOCKED: {media} is locked by another task ({exc}); will '
                'be retried next run.'
            ))
        except Exception:
            summary['errors'] += 1
            log.exception(
                f'medianest_backfill_plex_sidecars: error processing {media}'
            )
            self.stdout.write(self.style.ERROR(
                f'  FAILED: {media}: unexpected error, see the log'
            ))

    def _rename_media(self, media, summary, apply_changes, media_files):
        '''
            Moves the video to its profile path (apply) or reports whether
            it would (dry-run). Returns 'renamed', 'adopted' or 'in_place'
            when the media is (or would be) at its profile path, so its
            sidecars can be written there, and None otherwise.

            When the current file is gone but the profile path holds a
            regular file no other media claims, inside DOWNLOAD_ROOT and not
            a symlink, an earlier run moved it and then failed to save
            media_file (rename_files() moves before it saves), so the row
            is pointed at it ("adopted"). `media_files` is updated after
            every successful rename/adoption (both modes), so a later media
            processed in this same run cannot adopt or rename onto a target
            an earlier one just claimed here -- including, in dry-run, a
            target that is only projected, not yet on disk.

            rename_files() moves two sets of files after the video: every
            file next to it sharing its old stem (_sidecar_moves()) and,
            when the source's media_format contains `{key}`, every path
            anywhere under the source directory whose name contains the
            media's key (_key_matched_moves()). Both are checked here and
            the second set is listed, so a dry-run shows every move apply
            would make.

            Counted as an error, returning None: a downloaded row with no
            media_file; a missing current file with nothing to adopt; an
            adoption target that is a symlink or resolves outside
            DOWNLOAD_ROOT, or that belongs to another media (its video, a
            sidecar of one, or a destination an earlier rename in this run
            moves a file to, projected in a dry-run); a current or target
            directory that goes through a symlink below the storage
            location (rename_files() resolves both, so it would move
            sidecars these checks never saw and record the resolved path,
            not the target); a target video that already exists or that
            another media in this run already claimed (its video, or a
            sidecar destination of its rename, projected in a dry-run); a
            sidecar or key-match destination that is such a claimed path;
            a sidecar destination that already exists -- either one rename_files()
            would overwrite directly, or a target-side .nfo no move of this
            media's own would bring, which this command's own NFO write
            would otherwise silently overwrite right after the video moves
            (a target-side .jpg is fine: _handle_thumbnail() never
            overwrites one); an .nfo either move set would carry to the
            target NFO name that is not this media's own (it would be moved
            and then overwritten the same way); a file either move set would take that is
            another media's video, or a sidecar of one (rename_files()
            would move it without updating that media's row); a key match
            that is a directory; an already-in-place row whose video file
            is actually missing, or fails _path_problem() (a symlink, not a
            regular file, or resolving outside DOWNLOAD_ROOT); an
            already-in-place row, or an adopted
            half-finished move, with a same-key sidecar left behind outside
            its target directory (see _stray_sidecars()).
        '''
        if not media.media_file:
            self._media_error(
                summary, f'{media}: marked downloaded but has no media file; '
                'skipping its NFO and thumbnail',
            )
            return None
        current = Path(media.media_file.path)
        target = Path(media.filepath)
        if current == target:
            if not current.exists():
                self._media_error(
                    summary, f'{media}: already at its target path but the '
                    f'file is missing: {current}',
                )
                return None
            # The same checks a rename gets: its NFO and thumbnail are
            # written beside it next.
            path_problem = self._path_problem(current, target)
            if path_problem:
                self._media_error(
                    summary, f'{media}: not in place ({path_problem}); '
                    'skipping its NFO and thumbnail',
                )
                return None
            stray = self._stray_sidecars(
                media, target, self._stray_snapshot(media.source),
            )
            if stray:
                self._media_error(
                    summary, f'{media}: leftover sidecar(s) outside its target '
                    'directory, likely from a prior run that renamed the '
                    'video but failed partway through its own sidecar '
                    'moves (nothing moved or deleted; move or remove them '
                    'by hand once checked): ' +
                    ', '.join(str(path) for path in stray),
                )
                return None
            summary['already_in_place'] += 1
            return 'in_place'
        if not current.exists() and (
            target in self._projected_destinations
            or (
                _occupied(target)
                and self._claimed_by_other_media(
                    target, current, target, media_files,
                )
            )
        ):
            # Not an earlier half-finished move of this media's video: an
            # earlier rename in this run moves (or, in a dry-run, would
            # move) a file there, or it is another media's video or
            # sidecar. Adopting it would record that file as this video.
            self._media_error(
                summary, f'{media}: current file {current} is missing, and '
                f'its target {target} belongs to another media (its video '
                'or a sidecar, possibly moved there by this run); not '
                'adopting it',
            )
            return None
        if not current.exists() and target.exists() and target not in media_files:
            problem = self._adoption_problem(target)
            if problem is None:
                stray = self._stray_sidecars(
                    media, target, self._stray_snapshot(media.source),
                )
                if stray:
                    problem = (
                        'leftover sidecar(s) outside its target directory '
                        f'while adopting {target} (already moved from '
                        f'{current} by an earlier run that then failed '
                        'partway through its own sidecar moves; nothing '
                        'adopted, moved or deleted; move or remove them by '
                        'hand once checked): ' +
                        ', '.join(str(path) for path in stray)
                    )
            if problem is not None:
                self._media_error(summary, f'{media}: {problem}')
                return None
            if apply_changes:
                media.media_file.name = str(
                    target.relative_to(media.media_file.storage.location)
                )
                media.skip = False
                media.save(update_fields=('media_file', 'skip'))
            log.warning(
                f'medianest_backfill_plex_sidecars: {media}: adopting {target}, '
                f'already moved from {current} by an earlier run'
            )
            summary['adopted'] += 1
            media_files.discard(current)
            media_files.add(target)
            return 'adopted'
        problem = None
        storage_root = Path(media.media_file.storage.location)
        reserved = self._reserved_paths(current, target, media_files)
        moves = self._sidecar_moves(current, target)
        key_moves, key_collisions = self._key_matched_moves(
            media, current, target, moves, reserved,
        )
        occupied = [
            destination for other, destination in moves
            if destination != other
            and (_occupied(destination) or destination in reserved)
        ]
        # rename_files() rewrites the NFO at the target name right after
        # the video moves. So the file that will be there first must be
        # this media's own: a target-side .nfo no move brings, or an .nfo
        # a move carries there (an old-name one beside `current`, or a
        # key match), which would otherwise be moved and then overwritten.
        foreign_moved_nfos = []
        if media.source.write_nfo:
            nfo_path = self._sidecar_path(media, '.nfo')
            incoming = [
                other for other, destination in moves + key_moves
                if destination == nfo_path
            ]
            if not incoming and self._foreign_episode_nfo(media, nfo_path):
                occupied.append(nfo_path)
            foreign_moved_nfos = [
                other for other in incoming
                if self._foreign_episode_nfo(media, other)
            ]
        # Both move sets: a stem match can also be another media's sidecar
        # (foo.bar.srt of foo.bar.mkv beside foo.mkv), even when that
        # media's video is missing and so not moved with it.
        claimed = [
            other for other, _ in moves + key_moves
            if self._claimed_by_other_media(other, current, target, media_files)
        ]
        directories = [
            other for other, _ in moves + key_moves if other.is_dir()
        ]
        if not current.exists():
            problem = f'current file {current} is missing'
        elif (
            _occupied(target) or target in media_files
            or target in self._projected_destinations
        ):
            problem = f'target {target} is already occupied'
        elif (path_problem := self._path_problem(current, target)):
            problem = path_problem
        elif (linked := (
            self._symlinked_ancestor(current.parent, storage_root)
            or self._symlinked_ancestor(target.parent, storage_root)
        )):
            # rename_files() resolves both paths: it globs the old-stem
            # sidecars next to the resolved current file, where this
            # command's lexical ownership checks did not look, and it
            # records the resolved new path, so media_file would name the
            # link's target instead of `target` (a failed rename after the
            # move, then `target` occupied on every later run).
            problem = (
                f'its current or target directory goes through a symlink '
                f'({linked}); rename_files() resolves paths through it'
            )
        elif key_collisions:
            problem = (
                'key-matched path(s) whose destination is already taken, '
                'which rename_files() would leave behind: ' +
                ', '.join(str(path) for path in key_collisions)
            )
        elif claimed:
            problem = 'other media files would be moved with it: ' + ', '.join(
                str(path) for path in claimed
            )
        elif directories:
            problem = 'directories would be moved with it: ' + ', '.join(
                str(path) for path in directories
            )
        elif occupied:
            problem = 'sidecar target(s) already occupied: ' + ', '.join(
                str(path) for path in occupied
            )
        elif foreign_moved_nfos:
            problem = (
                "an NFO that is not this media's episode NFO would be moved "
                'to its new name and then overwritten: ' + ', '.join(
                    str(path) for path in foreign_moved_nfos
                )
            )
        if problem is None:
            for other, destination in key_moves:
                summary['key_matched_moves'] += 1
                self.stdout.write(
                    f'  {media}: key match {other} -> {destination}'
                )
        if problem is None and apply_changes:
            media.rename_files()
            if Path(media.media_file.path) != target:
                problem = f'rename to {target} did not happen'
        if problem is not None:
            self._media_error(
                summary, f'{media}: not renamed ({problem}); skipping its '
                'NFO and thumbnail',
            )
            return None
        summary['renamed'] += 1
        media_files.discard(current)
        media_files.add(target)
        self._projected_destinations.update(
            destination for _, destination in moves + key_moves
        )
        return 'renamed'

    def _reserved_paths(self, current, target, media_files):
        '''
            Paths an earlier media of this source already has, or (in a
            dry-run) is projected to have: other media's videos and the
            sidecar/key-match destinations of earlier renames. A move onto
            one is refused, so a dry-run predicts the same refusal apply
            meets once those files exist.
        '''
        return (media_files - {current, target}) | self._projected_destinations

    def _media_error(self, summary, message):
        summary['errors'] += 1
        log.error(f'medianest_backfill_plex_sidecars: {message}')
        self.stdout.write(self.style.ERROR(f'  FAILED: {message}'))

    def _path_problem(self, current, target):
        '''
            Why rename_files() must not move `current` to `target`, or
            None: it resolves `current` before moving it, so a symlinked
            current file could pull in a file from outside DOWNLOAD_ROOT,
            and a target directory that resolves outside DOWNLOAD_ROOT
            would take the video there and then fail to record it.
        '''
        if current.is_symlink():
            return f'current file {current} is a symlink'
        download_root = Path(settings.DOWNLOAD_ROOT).resolve()
        try:
            resolved = current.resolve(strict=True)
        except OSError as exc:
            return f'current file {current} cannot be resolved: {exc}'
        if not resolved.is_relative_to(download_root):
            return f'current file {current} resolves outside {download_root}'
        if not resolved.is_file():
            # Path.rename() would move a whole directory tree.
            return f'current file {current} is not a regular file'
        if not target.parent.resolve().is_relative_to(download_root):
            return f'target directory {target.parent} resolves outside {download_root}'
        return None

    def _reserved_path_problems(self, media, working_source, claimed):
        '''
            Why `media`'s paths collide with a path something else needs,
            as a list (empty when they do not), then records its paths in
            `claimed` (path -> owning media, shared across the source's
            media, late downloads included). Its video paths are the
            would-be target and, when recorded, the current file; its
            generated paths are the episode NFO (with write_nfo) and the
            thumbnail (with copy_thumbnails). Two media's videos meeting is
            left to the per-media rename checks.
        '''
        directory = Path(working_source.directory_path)
        target = Path(media.filepath)
        videos = {target}
        if media.media_file:
            videos.add(Path(media.media_file.path))
        generated = {}
        if working_source.write_nfo:
            generated['episode NFO'] = self._sidecar_path(media, '.nfo')
        if working_source.copy_thumbnails:
            generated['thumbnail'] = self._sidecar_path(media, '.jpg')
        problems = []
        nfo = generated.get('episode NFO')
        if nfo is not None and nfo == directory / 'tvshow.nfo':
            problems.append(f"its episode NFO would be the show's {nfo}")
        if nfo is not None and nfo == target:
            problems.append(f'its episode NFO would be the video file itself ({nfo})')
        if working_source.copy_channel_images:
            images = {directory / name for name in _SOURCE_IMAGE_NAMES}
            for path in sorted((videos | set(generated.values())) & images):
                problems.append(
                    f'the channel image download would overwrite {path}'
                )
        for kind, path in generated.items():
            owner = claimed.get(path)
            if owner is not None and owner != str(media):
                problems.append(f'its {kind} {path} is also used by {owner}')
        for path in videos:
            owner = claimed.get(path)
            if owner is not None and owner != str(media) and (
                path in self._claimed_generated
            ):
                problems.append(f'its video {path} is a sidecar of {owner}')
        for path in videos | set(generated.values()):
            claimed.setdefault(path, str(media))
        self._claimed_generated.update(generated.values())
        return problems

    def _special_tree_entries(self, directory):
        '''
            Every entry at or under `directory` that is a symlink (live or
            dangling) or neither a regular file nor a directory (a FIFO,
            socket or device), without following links, sorted. Empty when
            `directory` does not exist.
        '''
        if directory.is_symlink():
            return [directory]
        if not directory.is_dir():
            return []
        special = []
        for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
            for name in dirnames + filenames:
                path = Path(dirpath) / name
                mode = os.lstat(path).st_mode
                if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    special.append(path)
        return sorted(special)

    def _nested_source_directories(self, source, working_source):
        '''
            The directories of other sources that resolve to
            `working_source`'s own directory, lie inside it, or contain it:
            rename_files()'s old-stem glob beside each recorded video (a
            recorded path can already sit in a nested directory) and the
            {key} sweep of the whole tree can reach a nested source's
            files, and a parent source's files can sit beside this
            source's videos, where the same glob takes them for sidecars.
            Any overlap is refused regardless of media_format.
        '''
        top = Path(working_source.directory_path).resolve()
        overlapping = []
        for other in Source.objects.exclude(pk=source.pk):
            other_dir = Path(other.directory_path).resolve()
            if other_dir.is_relative_to(top) or top.is_relative_to(other_dir):
                overlapping.append(str(other.directory_path))
        return sorted(overlapping)

    def _symlinked_ancestor(self, directory, storage_root):
        '''
            The first symlink among `directory` and its parents below
            `storage_root` (the media storage location, which
            rename_files() records paths relative to), or None.
        '''
        for path in (directory, *directory.parents):
            if path == storage_root or not path.is_relative_to(storage_root):
                return None
            if path.is_symlink():
                return path
        return None

    def _foreign_episode_nfo(self, media, nfo_path):
        '''
            True when `nfo_path` holds something other than this media's
            own episode NFO (an `<episodedetails>` whose `<id>` or
            `<uniqueid>` is this media's key), which must not be
            overwritten. A symlink (even a dangling one) is never this
            command's own NFO: writing would replace the link itself.
            Neither is anything else that is not a regular file (a
            directory, or a FIFO, which a read would block on).
        '''
        if nfo_path.is_symlink():
            return True
        if not nfo_path.exists():
            return False
        if not nfo_path.is_file():
            # A directory or FIFO: reading it would fail or block.
            return True
        raw = nfo_path.read_bytes()
        if not raw:
            return False
        try:
            root = ElementTree.fromstring(raw)
        except (ElementTree.ParseError, LookupError, ValueError):
            # LookupError/ValueError: an XML declaration naming an encoding
            # expat cannot read (encoding="ANSI", encoding="UTF-32").
            return True
        key = str(media.key).strip()
        return root.tag != 'episodedetails' or not any(
            (element.text or '').strip() == key
            for element in root
            if element.tag in ('id', 'uniqueid')
        )

    def _adoption_problem(self, target):
        '''
            Why the file at `target` must not be adopted, or None. Mirrors
            rename_files()'s own resolve(strict=True): a symlink, or a path
            that resolves outside DOWNLOAD_ROOT, is never pointed at.
        '''
        if target.is_symlink():
            return f'not adopting {target}: it is a symlink'
        try:
            resolved = target.resolve(strict=True)
        except OSError as exc:
            return f'not adopting {target}: {exc}'
        download_root = Path(settings.DOWNLOAD_ROOT).resolve()
        if not resolved.is_relative_to(download_root):
            return f'not adopting {target}: it resolves outside {download_root}'
        if not resolved.is_file():
            return f'not adopting {target}: it is not a regular file'
        return None

    def _claimed_by_other_media(self, path, current, target, media_files):
        '''
            True when `path` is another media's video, or a sidecar named
            after one (its stem, a ".", then suffixes, in the same
            directory) -- where that video is now (`media_files`) or was
            before this run (a dry-run does not move the sidecars of media
            it has already processed).
        '''
        own = {current, target}
        if path in media_files and path not in own:
            return True
        for video in media_files | self._original_media_files:
            if video in own or video.parent != path.parent:
                continue
            (_, stem) = directory_and_stem(video)
            if path.name.startswith(stem + '.'):
                return True
        return False

    def _key_matched_moves(
        self, media, current, target, sidecar_moves, reserved=frozenset(),
    ):
        '''
            The (path, destination) pairs rename_files()'s second pass
            would move: with `{key}` in the source's media_format it takes
            every path under the source directory whose name contains the
            media's key (Path.rglob, so files and directories alike), and
            moves it next to the new video under the new stem plus the
            path's own suffixes -- skipping the video itself, paths the
            stem pass already moved and paths already at their destination.
            Returns (moves, collisions): a path whose destination exists or
            an earlier move takes (the stem pass's destinations count too)
            is skipped by rename_files() and so left behind under its old
            name; those are returned as collisions, as is one whose
            destination is in `reserved` (_reserved_paths()).
        '''
        if '{key}' not in str(media.source.media_format):
            return [], []
        top_dir = Path(media.source.directory_path)
        if not top_dir.is_dir():
            return [], []
        (new_dir, new_stem) = directory_and_stem(target)
        stem_moved = {other for other, _ in sidecar_moves}
        taken = {target} | {destination for _, destination in sidecar_moves}
        taken |= reserved
        moves = []
        collisions = []
        for path in sorted(top_dir.rglob('*' + glob_quote(str(media.key)) + '*')):
            if path == current or path in stem_moved:
                continue
            (_, path_stem) = directory_and_stem(path, True)
            destination = new_dir / (new_stem + path.name[len(path_stem):])
            if destination == path:
                continue
            if destination in taken or _occupied(destination):
                collisions.append(path)
                continue
            taken.add(destination)
            moves.append((path, destination))
        return moves, collisions

    def _stray_snapshot(self, source):
        '''
            A one-time snapshot (a list of every file Path under
            `source`'s directory) reused by every already-in-place/adopted
            media of this SAME source in this run, instead of walking the
            whole tree (Path.rglob) again for each one -- one tree walk
            per source instead of one per already-in-place/adopted media.

            Safe: _stray_sidecars() finds a media's own leftovers by a
            substring match on THAT media's own `key`, and processing a
            DIFFERENT media in this same run never creates or removes a
            file carrying this media's key -- only that media's own
            processing could do that, and _stray_sidecars() is only ever
            consulted for a media before anything of ITS OWN has moved
            (the already-in-place branch moves nothing; the adopted
            branch's own action, once this check clears, is a DB update,
            not a filesystem move). So a snapshot taken once, lazily, the
            first time any media of this source needs it stays accurate
            for the rest of the source's per-media loop.

            Cached by `source.pk` (built only on first use, so a source
            whose media are all being renamed for the first time -- never
            hitting the already-in-place/adopted branches -- pays
            nothing).
        '''
        cached = self._stray_snapshot_cache.get(source.pk)
        if cached is not None:
            return cached
        source_dir = Path(source.directory_path)
        snapshot = (
            [path for path in source_dir.rglob('*') if path.is_file()]
            if source_dir.is_dir() else []
        )
        self._stray_snapshot_cache[source.pk] = snapshot
        return snapshot

    def _stray_sidecars(self, media, target, snapshot):
        '''
            Files elsewhere under the source directory (from `snapshot`,
            a pre-built per-source listing -- see _stray_snapshot()) whose
            name contains this media's own `key` -- left behind when a
            prior run's rename_files() moved the video, saved media_file,
            and then raised partway through moving an old-name sidecar (a
            subtitle, a JSON file, or a bare thumbnail with no cache
            record). Once media_file already points at `target`, the old
            path (and the media_format that produced it, bracketed or
            not) is no longer known, so this is a best-effort, name-based
            scan rather than a move: it makes the leftover loudly visible
            (counted as an error) instead of silently leaving it orphaned
            under the old name forever. Only the target's own completed
            sidecars are excluded: in its directory, named its stem plus
            a "." (the NFO, thumbnail, subtitles and info.json all are).
            So an old-stem leftover beside a target that kept its
            directory is found too, even one whose old stem starts with
            the new stem.
        '''
        key = str(media.key)
        (target_dir, target_stem) = directory_and_stem(target)
        return sorted(
            path for path in snapshot
            if key in path.name and path != target and not (
                path.parent == target_dir
                and path.name.startswith(target_stem + '.')
            )
        )

    def _sidecar_moves(self, current, target):
        '''
            The (file, destination) pairs rename_files() would move: every
            file next to `current` that shares its stem goes to the matching
            name next to `target`, with Path.replace(), which silently
            replaces an existing file there.
        '''
        (old_dir, old_stem) = directory_and_stem(current)
        (new_dir, new_stem) = directory_and_stem(target)
        return [
            (other, new_dir / (new_stem + other.name[len(old_stem):]))
            for other in sorted(old_dir.glob(glob_quote(old_stem) + '*'))
            if other != current
        ]

    def _sidecar_path(self, media, suffix):
        '''
            `media`'s NFO/thumbnail path under its profile filename.
            Media.nfopath/thumbpath name sidecars after the CURRENT file
            once downloaded, which in dry-run is still the old name; this
            uses the profile filename in both modes (identical after a
            real rename).
        '''
        prefix = os.path.splitext(os.path.basename(media.filename))[0]
        return media.directory_path / f'{prefix}{suffix}'

    def _handle_episode_nfo(self, media, summary, apply_changes, renamed=False):
        '''
            Writes (apply) or predicts the episode NFO. After a rename in
            apply mode rename_files() has already written it (it rewrites
            the NFO whenever write_nfo is on), so matching bytes still
            count as written there -- the same count a dry-run predicts.
        '''
        if not media.source.write_nfo:
            return
        nfo_path = self._sidecar_path(media, '.nfo')
        content = media.nfoxml
        # is_file(), not exists(): reading a FIFO would block.
        if nfo_path.is_file() and nfo_path.read_bytes() == content.encode('utf-8'):
            if renamed and apply_changes:
                summary['nfo_written'] += 1
            else:
                summary['nfo_unchanged'] += 1
            return
        if self._foreign_episode_nfo(media, nfo_path):
            self._media_error(
                summary, f'{media}: not overwriting {nfo_path}: it is not '
                "this media's episode NFO",
            )
            return
        if apply_changes:
            write_text_file(nfo_path, content)
        summary['nfo_written'] += 1

    def _handle_thumbnail(self, media, summary, apply_changes):
        if not media.source.copy_thumbnails:
            return
        # Only ever copy a thumbnail that is ALREADY downloaded locally --
        # Media.copy_thumbnail() itself synchronously fetches one over the
        # network (via download_media_image.call_local()) when it is not,
        # which this command must never trigger.
        if not media.thumb_file_exists:
            return
        # thumb_file_exists only checks existence: copying from a
        # directory would raise after the video moved, and from a FIFO
        # would block. Skip it (both modes) unless it is a regular file.
        try:
            cached = Path(media.thumb.path)
        except ValueError:
            cached = None  # no file recorded; copy_thumbnail() decides
        if cached is not None and cached.exists() and not cached.is_file():
            self.stdout.write(self.style.WARNING(
                f'  NOTE: {media}: cached thumbnail {cached} is not a '
                'regular file; not copying it'
            ))
            return
        thumb_path = self._sidecar_path(media, '.jpg')
        # A dry-run has not moved the old-stem .jpg its rename projects
        # there; apply has, and finds it occupied. Count both the same.
        if _occupied(thumb_path) or thumb_path in self._projected_destinations:
            return
        if apply_changes:
            media.copy_thumbnail()
        summary['thumbs_copied'] += 1

    def _process_tvshow_and_images(
        self, source, apply_changes, summary, images_already_queued=False,
        overlay_changed=False,
    ):
        '''
            Writes (apply) or predicts (dry-run) `source`'s tvshow.nfo,
            then enqueues (or predicts enqueueing) `download_source_images`
            when `copy_channel_images` is on and poster.jpg is still
            missing, the source directory exists (or, in a dry-run, a save
            would create it) and no file the job writes is a symlink (see
            _image_write_problems()) -- unless `images_already_queued` says
            source_pre_save's own copy_channel_images-turned-on check
            already scheduled it for this same save, in which case this
            command must not also schedule a second job, but still counts
            it either way so dry-run's prediction and apply's actual
            behaviour report the same `images_enqueued` count. When
            `images_already_queued` is true, the caller
            (`_process_source()`) has already refused the source before
            saving it if `_unsafe_image_queue_problems()` found a symlinked
            image destination or a directory outside DOWNLOAD_ROOT, so
            `unsafe` below is never nonempty on that path.

            A source directory resolving outside DOWNLOAD_ROOT is an error
            in both modes and nothing is written or queued: write_text_file
            creates its temporary file in the target directory before its
            own containment check, so the tvshow.nfo write would put a file
            outside the root before failing. This still covers a source
            whose overlay does not turn `copy_channel_images` on (already
            on beforehand): that case is not covered by the
            `images_already_queued` gate above.
        '''
        directory = Path(source.directory_path)
        if self._resolves_outside_download_root(directory):
            download_root = Path(settings.DOWNLOAD_ROOT).resolve()
            if images_already_queued:
                # source_pre_save queued it with the save; count it as the
                # normal path does.
                summary['images_enqueued'] += 1
            summary['errors'] += 1
            message = (
                f'{source}: source directory {directory} resolves outside '
                f'{download_root}; not writing tvshow.nfo or queueing '
                'channel images'
            )
            log.error(f'medianest_backfill_plex_sidecars: {message}')
            self.stdout.write(self.style.ERROR(f'  FAILED: {message}'))
            return
        if apply_changes:
            try:
                if write_tvshow_nfo(source, raise_errors=True):
                    summary['tvshow_written'] += 1
            except Exception:
                summary['errors'] += 1
                message = f'{source}: failed to write tvshow.nfo, see the log'
                log.exception(f'medianest_backfill_plex_sidecars: {message}')
                self.stdout.write(self.style.ERROR(f'  FAILED: {message}'))
        else:
            # An overlay field change means apply's form.save() below would
            # fire source_pre_save, which synchronously creates a missing
            # source directory (check_source_directory_exists) before
            # write_tvshow_nfo() runs -- assume that here too, or a source
            # whose directory does not exist yet would predict no write
            # while apply goes on to create one.
            needs_write = tvshow_nfo_needs_write(
                source, assume_directory_exists=overlay_changed,
            )
            if needs_write:
                summary['tvshow_written'] += 1

        poster_path = Path(source.directory_path) / 'poster.jpg'
        poster_exists = _occupied(poster_path)
        # The image download writes every _SOURCE_IMAGE_NAMES file with a
        # plain open(), which follows a symlink (even a dangling one) and
        # writes wherever it points, and never creates the directory. This
        # command never queues it then. A dry-run whose overlay changes a
        # field assumes the directory source_pre_save creates on save.
        unsafe = (
            self._image_write_problems(
                source,
                assume_directory_exists=overlay_changed and not apply_changes,
            )
            if source.copy_channel_images else []
        )
        if unsafe:
            self.stdout.write(self.style.WARNING(
                '  NOTE: not queueing the channel image download: ' +
                '; '.join(unsafe) + '.'
            ))
        if images_already_queued and poster_exists:
            # source_pre_save queues it whatever is on disk, and it writes
            # the images unconditionally.
            self.stdout.write(self.style.WARNING(
                '  NOTE: turning copy_channel_images on queues TubeSync\'s '
                'own image download, which replaces the existing '
                'poster/banner/thumbnail images.'
            ))
        if source.copy_channel_images and (
            images_already_queued or (not poster_exists and not unsafe)
        ):
            if apply_changes:
                if not images_already_queued:
                    # TaskHistory.schedule(..., remove_duplicates=True) --
                    # same mechanism sync/signals.py's own
                    # save_all_media_for_source/index_source scheduling
                    # uses -- rather than calling download_source_images()
                    # directly, so a job already pending for this source
                    # (from a previous --apply re-run against a still-
                    # missing poster.jpg, or from source_pre_save's own
                    # enqueue) is revoked instead of piling up a duplicate
                    # (common/huey.py::on_executing_remove_duplicates()).
                    # Deliberately NOT .call_local(), which would run the
                    # real network image fetch synchronously inside this
                    # command. Skipped entirely when source_pre_save's own
                    # copy_channel_images-turned-on check already enqueued
                    # it this save -- but still counted below either way.
                    TaskHistory.schedule(
                        download_source_images,
                        str(source.pk),
                        remove_duplicates=True,
                        vn_fmt=_('Downloading images for source "{}"'),
                        vn_args=(source.name,),
                    )
            summary['images_enqueued'] += 1

    def _image_write_problems(self, source, assume_directory_exists=False):
        '''
            Why `download_source_images` must not run for `source`, as a
            list (empty when it may): its directory does not exist (the
            task opens its files without creating it, so it would fail and
            retry), or one of the files it writes is a symlink, which it
            would write through, or something other than a regular file (a
            directory makes open() fail and the task retry; a FIFO can
            block it). The caller has already refused a
            directory resolving outside DOWNLOAD_ROOT.
        '''
        directory = Path(source.directory_path)
        if not directory.is_dir() and not assume_directory_exists:
            return [
                f'{directory} does not exist and nothing in this run '
                'creates it'
            ]
        problems = []
        for name in _SOURCE_IMAGE_NAMES:
            path = directory / name
            if path.is_symlink():
                problems.append(f'{path} is a symlink, which it would write through')
            elif path.exists() and not path.is_file():
                problems.append(f'{path} exists but is not a regular file')
        return problems

    def _resolves_outside_download_root(self, directory):
        '''
            True when `directory` (a Path) resolves outside DOWNLOAD_ROOT.
        '''
        download_root = Path(settings.DOWNLOAD_ROOT).resolve()
        return not directory.resolve().is_relative_to(download_root)

    def _unsafe_image_queue_problems(self, source):
        '''
            Why an overlay save that turns `source.copy_channel_images` on
            must be refused BEFORE it is saved, as a list (empty when it is
            safe): source_pre_save queues `download_source_images`
            unconditionally on that save, and the task opens every file it
            writes with a plain open(..., 'wb'), which follows a symlink
            (even a dangling one) and writes through it -- so this must run
            before `_save_overlay()`, not after. A missing directory is
            fine here: the save's own pre_save creates it before the task
            runs, so `_image_write_problems()` is called with
            `assume_directory_exists=True`.
        '''
        directory = Path(source.directory_path)
        problems = []
        if self._resolves_outside_download_root(directory):
            download_root = Path(settings.DOWNLOAD_ROOT).resolve()
            problems.append(
                f'source directory {directory} resolves outside '
                f'{download_root}'
            )
        problems.extend(
            self._image_write_problems(source, assume_directory_exists=True)
        )
        return problems

    def _print_summary(self, summary, apply_changes):
        self.stdout.write('')
        self.stdout.write(f'Summary ({"apply" if apply_changes else "dry-run"}):')
        for field in _SUMMARY_FIELDS:
            self.stdout.write(f'  {field}: {summary[field]}')
