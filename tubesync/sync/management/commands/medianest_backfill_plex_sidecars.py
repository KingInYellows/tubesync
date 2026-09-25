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
        modes (_overlay_form()). Dry-run binds it to an in-memory copy of
        the source and points media.source at that copy, so
        `media.filepath`/`media.nfoxml` etc. reflect the WOULD-BE values
        without saving anything.
      - In apply mode, the source is saved (only when a field actually
        changes) before the per-media loop runs, so the same property
        reads reflect the real new values.
      - Sidecar paths come from the profile filename in both modes
        (_sidecar_path()), and tvshow.nfo uses the same
        tvshow_nfo_needs_write() decision the real write does.

    Failure handling: each media is renamed in its own transaction and
    its NFO/thumbnail are written afterwards, so a sidecar failure cannot
    roll back the media_file update of a file that already moved. A
    missing current file or an occupied target is an error (the media's
    sidecars are skipped), each source is isolated from the others, and
    the command exits non-zero when anything errored or was skipped as
    locked -- re-run it once the cause is fixed. --apply refuses to run
    unless the effective user owns DOWNLOAD_ROOT, so everything it
    creates stays writable by TubeSync (`docker exec -u app ...`).

    Never deletes any file (grep this module: no unlink/rmtree/os.remove
    call). `Media.rename_files()`'s own empty-directory cleanup (an
    `rmdir()` on now-empty leftover directories after a move) is existing
    upstream behavior, not something this command adds or could disable;
    it never removes a non-empty directory or a file.

    Saving the source (via SourceForm, same as a real edit) fires
    TubeSync's own Source post_save signal
    (sync/signals.py::source_post_save), which unconditionally schedules
    save_all_media_for_source -- and that task, if a huey consumer is
    running and later processes it, schedules rename_all_media_for_source
    in turn. Both are asynchronous (huey-enqueued, not executed by this
    command) and both are harmless alongside this command's own
    synchronous work: rename_all_media_for_source is gated by the
    RENAME_SOURCES/RENAME_ALL_SOURCES settings before doing anything (most
    deployments run with neither enabled, so it is a pure no-op), and even
    where it is enabled, Media.rename_files() is itself idempotent --
    calling it again after this command already renamed everything simply
    returns immediately (`old_video_path == new_video_path`). In tests,
    huey enqueue-without-a-consumer never actually executes anything (see
    the wider test suite's existing reliance on this same fact for
    signals.py's own enqueue calls) -- these tests never see this cascade
    actually run.
'''
import copy
import os
from pathlib import Path
from uuid import UUID

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.forms.models import model_to_dict
from django_huey import lock_task as huey_lock_task
from huey.exceptions import TaskLockedException

from common.logger import log
from medianest_bridge.config import load_validated_source_defaults
from medianest_bridge.source_forms import (
    _coerce_list_shaped_fields, extract_form_errors, run_edit_source_checks,
)
from sync.choices import TaskQueue, Val, YouTube_SourceType
from sync.forms import SourceForm
from sync.models import Media, Source
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
    'sources', 'media_seen', 'renamed', 'already_in_place',
    'nfo_written', 'nfo_unchanged', 'thumbs_copied',
    'tvshow_written', 'images_enqueued', 'locked', 'errors',
)


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
        apply_changes = options['apply']

        defaults_by_type, config_errors = load_validated_source_defaults()
        if config_errors:
            raise CommandError(
                'MEDIANEST_BRIDGE_SOURCE_DEFAULTS is invalid; refusing to '
                'change anything:\n' +
                '\n'.join(f'  - {message}' for message in config_errors)
            )
        if apply_changes:
            self._check_running_as_download_owner()

        sources = self._resolve_sources(options)
        if not sources:
            self.stdout.write('No matching sources found.')
            return

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
        if summary['errors'] or summary['locked']:
            raise CommandError(
                f'{summary["errors"]} error(s) and {summary["locked"]} '
                'locked media; see the output above. Nothing was deleted; '
                're-run once the cause is fixed (locked media are retried '
                'by the next run).'
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

        overlay = defaults_by_type.get(contract_type, {})
        working_source = source
        images_already_queued = False
        if overlay:
            changes = self._overlay_changes(source, overlay)
            self._describe_overlay_diff(source, changes)
            # Dry-run validates against a copy, so it reports exactly the
            # failures --apply would hit without touching `source`.
            form = self._overlay_form(
                source if apply_changes else copy.copy(source), overlay,
            )
            if not form.is_valid():
                summary['errors'] += 1
                messages = '; '.join(extract_form_errors(form))
                log.error(
                    f'medianest_backfill_plex_sidecars: {source}: T3 profile '
                    f'failed validation: {messages}'
                )
                self.stdout.write(self.style.ERROR(f'  SKIPPED: {messages}'))
                return
            # Turning copy_channel_images on makes source_pre_save enqueue
            # download_source_images itself.
            images_already_queued = bool(changes.get('copy_channel_images'))
            if apply_changes:
                # Saving when no overlay field changes would still fire
                # source_post_save and its save_all_media_for_source
                # cascade on every re-run.
                if changes:
                    form.save()
            else:
                working_source = form.instance

        downloaded_qs = Media.objects.filter(
            source=source, downloaded=True,
        ).order_by('key')
        for media in downloaded_qs:
            summary['media_seen'] += 1
            if not apply_changes:
                media.source = working_source
            self._process_media(media, apply_changes, summary)

        self._process_tvshow_and_images(
            working_source, apply_changes, summary, images_already_queued,
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
            does), so dry-run passes a copy.
        '''
        data = model_to_dict(source, fields=list(SourceForm.base_fields.keys()))
        for field, value in data.items():
            # A saved CommaSepChoiceField (sponsorblock_categories) loads as
            # a CommaSepChoice tuple, not the list its form field expects.
            if hasattr(value, 'selected_choices'):
                data[field] = list(value.selected_choices)
        data.update(overlay)
        _coerce_list_shaped_fields(data)
        form = SourceForm(data=data, instance=source)
        if form.is_valid():
            run_edit_source_checks(form)
        return form

    def _overlay_changes(self, source, overlay):
        '''The overlay fields whose value differs from `source`'s.'''
        return {
            field: value for field, value in overlay.items()
            if getattr(source, field, None) != value
        }

    def _describe_overlay_diff(self, source, changes):
        if not changes:
            self.stdout.write('  T3 profile already applied (no field changes).')
            return
        self.stdout.write('  T3 profile field changes:')
        for field in sorted(changes):
            self.stdout.write(
                f'    {field}: {getattr(source, field, None)!r} -> {changes[field]!r}'
            )

    def _process_media(self, media, apply_changes, summary):
        try:
            if apply_changes:
                with (
                    huey_lock_task(
                        f'index_media:{media.uuid}', queue=Val(TaskQueue.FS),
                    ),
                    huey_lock_task(
                        f'media:{media.uuid}', queue=Val(TaskQueue.DB),
                    ),
                ):
                    # Only the rename is transactional, like upstream's
                    # rename_all_media_for_source: a later sidecar failure
                    # must never roll back the media_file update of a file
                    # that has already moved on disk.
                    with transaction.atomic(durable=False):
                        in_place = self._rename_media(media, summary, True)
                    if in_place:
                        self._handle_episode_nfo(media, summary, True)
                        self._handle_thumbnail(media, summary, True)
            elif self._rename_media(media, summary, False):
                self._handle_episode_nfo(media, summary, False)
                self._handle_thumbnail(media, summary, False)
        except TaskLockedException:
            summary['locked'] += 1
            log.warning(
                f'medianest_backfill_plex_sidecars: {media} is locked by '
                'another task; skipping it this run.'
            )
        except Exception:
            summary['errors'] += 1
            log.exception(
                f'medianest_backfill_plex_sidecars: error processing {media}'
            )

    def _rename_media(self, media, summary, apply_changes):
        '''
            Moves the video to its profile path (apply) or reports whether
            it would (dry-run). Returns True when the media is (or would
            be) at its profile path, so its sidecars can be written there.
            A missing current file, or a target already occupied by
            another file, is counted as an error and returns False:
            rename_files() silently declines to move in both cases, and
            writing sidecars would then name them after the wrong file.
        '''
        if not (media.downloaded and media.media_file):
            summary['already_in_place'] += 1
            return True
        current = Path(media.media_file.path)
        target = Path(media.filepath)
        if current == target:
            summary['already_in_place'] += 1
            return True
        problem = None
        if not current.exists():
            problem = f'current file {current} is missing'
        elif target.exists():
            problem = f'target {target} is already occupied'
        if problem is None and apply_changes:
            media.rename_files()
            if Path(media.media_file.path) != target:
                problem = f'rename to {target} did not happen'
        if problem is not None:
            summary['errors'] += 1
            log.error(
                f'medianest_backfill_plex_sidecars: {media}: not renamed '
                f'({problem}); skipping its NFO and thumbnail'
            )
            return False
        summary['renamed'] += 1
        return True

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

    def _handle_episode_nfo(self, media, summary, apply_changes):
        if not media.source.write_nfo:
            return
        nfo_path = self._sidecar_path(media, '.nfo')
        content = media.nfoxml
        if nfo_path.exists() and nfo_path.read_bytes() == content.encode('utf-8'):
            summary['nfo_unchanged'] += 1
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
        if self._sidecar_path(media, '.jpg').exists():
            return
        if apply_changes:
            media.copy_thumbnail()
        summary['thumbs_copied'] += 1

    def _process_tvshow_and_images(
        self, source, apply_changes, summary, images_already_queued=False,
    ):
        if apply_changes:
            try:
                if write_tvshow_nfo(source, raise_errors=True):
                    summary['tvshow_written'] += 1
            except Exception:
                summary['errors'] += 1
                log.exception(
                    'medianest_backfill_plex_sidecars: failed to write '
                    f'tvshow.nfo for {source}'
                )
        elif tvshow_nfo_needs_write(source):
            summary['tvshow_written'] += 1

        poster_path = Path(source.directory_path) / 'poster.jpg'
        if (
            source.copy_channel_images
            and not poster_path.exists()
            and not images_already_queued
        ):
            if apply_changes:
                # Enqueues the huey task (django_huey's db_task decorator
                # makes a normal call schedule it) -- deliberately NOT
                # .call_local(), which would run the real network image
                # fetch synchronously inside this command.
                download_source_images(
                    str(source.pk),
                    delay=download_source_images.settings.get('delay'),
                )
            summary['images_enqueued'] += 1

    def _print_summary(self, summary, apply_changes):
        self.stdout.write('')
        self.stdout.write(f'Summary ({"apply" if apply_changes else "dry-run"}):')
        for field in _SUMMARY_FIELDS:
            self.stdout.write(f'  {field}: {summary[field]}')
