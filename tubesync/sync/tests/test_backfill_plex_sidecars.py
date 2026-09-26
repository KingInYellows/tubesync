'''
    T4: `medianest_backfill_plex_sidecars` management command.

    Builds bridge-style ("acq-src-...") sources with the OLD upstream
    default media_format and T3's flags all off, downloads a dummy .mkv
    for one Media each (the checked-in "boring" metadata fixture), then
    exercises dry-run, apply, and idempotency against the T3 built-in
    profile (MEDIANEST_BRIDGE_SOURCE_DEFAULTS left unset).

    All filesystem writes go through temp_download_root() (both
    sync.models._migrations.media_file_storage.location and
    settings.DOWNLOAD_ROOT patched to the same tempfile.TemporaryDirectory())
    -- never the real DOWNLOAD_ROOT/downloads. Both are needed because
    Media.filepath/rename_files() resolve paths through the storage
    location while write_text_file()'s allow-list checks
    settings.DOWNLOAD_ROOT.
'''
import copy
import logging
import os
import tempfile
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from unittest.mock import PropertyMock, patch
from xml.etree import ElementTree

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django_huey import lock_task as huey_lock_task
from huey.exceptions import TaskLockedException

from medianest_bridge import source_forms as medianest_source_forms
from medianest_bridge.config import source_defaults
from sync.choices import (
    TaskQueue, Val, Fallback, SourceResolution,
    YouTube_AudioCodec, YouTube_VideoCodec,
    YouTube_SourceType,
)
from sync.management.commands.medianest_backfill_plex_sidecars import (
    Command as BackfillCommand,
)
from sync.models import Media, Source
from sync.models._migrations import media_file_storage
from sync.tasks import download_source_images
from sync.tvshow_nfo import _clear_show_title_cache

from .fixtures import all_test_metadata

# title 'no fancy stuff title', upload_date 2017-09-11
metadata = all_test_metadata['boring']


@contextmanager
def temp_download_root():
    with tempfile.TemporaryDirectory() as tmp_dir:
        with (
            override_settings(DOWNLOAD_ROOT=tmp_dir),
            patch.object(media_file_storage, 'location', tmp_dir),
        ):
            yield tmp_dir


def make_bridge_source(**overrides):
    '''A source shaped like the bridge would create it BEFORE T3: old
    default media_format, every T3 flag off.'''
    defaults = dict(
        source_type=Val(YouTube_SourceType.CHANNEL_ID),
        key='UCabcdefghijklmnopqrstuv',
        name='acq-src-UCabcdefghijklmnopqrstuv',
        directory='acq-src-UCabcdefghijklmnopqrstuv',
        media_format=settings.MEDIA_FORMATSTR_DEFAULT,
        write_nfo=False,
        copy_thumbnails=False,
        copy_channel_images=False,
        index_schedule=3600,
        delete_old_media=False,
        days_to_keep=14,
        source_resolution=Val(SourceResolution.VIDEO_1080P),
        source_vcodec=Val(YouTube_VideoCodec.VP9),
        source_acodec=Val(YouTube_AudioCodec.OPUS),
        prefer_60fps=False,
        prefer_hdr=False,
        fallback=Val(Fallback.FAIL),
    )
    defaults.update(overrides)
    return Source.objects.create(**defaults)


def download_dummy_file(media):
    '''
        Creates a dummy .mkv on disk at media's CURRENT (old-format)
        filepath and marks it downloaded, matching upstream's own
        media_file/downloaded bookkeeping (rename_files() only moves an
        already-downloaded item with a real media_file).
    '''
    old_path = media.filepath
    old_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.write_bytes(b'fake-mkv-bytes')
    media.media_file.name = str(old_path.relative_to(media_file_storage.location))
    media.downloaded = True
    media.save()
    return old_path


def run_backfill(*args, **options):
    out = StringIO()
    call_command('medianest_backfill_plex_sidecars', *args, stdout=out, **options)
    return out.getvalue()


def run_backfill_capture(*args, **options):
    '''
        Same as run_backfill(), but returns (output, exception) instead of
        letting a CommandError propagate -- lets a test inspect stdout
        (per-media/per-source FAILED/SKIPPED/NOTE lines) even when the
        command exits non-zero.
    '''
    out = StringIO()
    exc = None
    try:
        call_command('medianest_backfill_plex_sidecars', *args, stdout=out, **options)
    except CommandError as caught:
        exc = caught
    return out.getvalue(), exc


def summary_of(output):
    '''The summary counts of a run's output, without the mode header.'''
    return output.split('Summary (', 1)[1].split('\n', 1)[1]


class BackfillPlexSidecarsTestCase(TestCase):

    def setUp(self):
        logging.disable(logging.CRITICAL)
        # resolve_show_title()'s process-local cache is keyed by
        # source.pk, but every test here creates fresh sources anyway --
        # cleared defensively so a title never leaks between tests.
        _clear_show_title_cache()

    def test_dry_run_changes_nothing(self):
        with temp_download_root():
            source = make_bridge_source()
            source.make_directory()
            media = Media.objects.create(key='vid1', source=source, metadata=metadata)
            old_path = download_dummy_file(media)
            old_media_format = source.media_format

            output = run_backfill('--source', str(source.uuid))

            self.assertIn('dry-run', output)
            source.refresh_from_db()
            self.assertEqual(source.media_format, old_media_format)
            self.assertFalse(source.write_nfo)
            self.assertTrue(old_path.exists())
            media.refresh_from_db()
            self.assertEqual(
                str(media.media_file),
                str(old_path.relative_to(media_file_storage.location)),
            )
            self.assertFalse((source.directory_path / 'tvshow.nfo').exists())

    def test_dry_run_predicts_tvshow_write_when_apply_would_create_the_directory(self):
        '''
            No source.make_directory() here: the built-in profile's
            write_nfo=True overlay change means apply's form.save() would
            create the still-missing directory via source_pre_save before
            writing tvshow.nfo. Dry-run must predict that write instead of
            reporting none just because the directory does not exist yet.
        '''
        with temp_download_root():
            source = make_bridge_source()
            self.assertFalse(source.directory_path.exists())

            dry = run_backfill('--source', str(source.uuid))
            self.assertIn('tvshow_written: 1', dry)

            applied = run_backfill('--source', str(source.uuid), '--apply')
            self.assertIn('tvshow_written: 1', applied)
            self.assertTrue((source.directory_path / 'tvshow.nfo').exists())

    def test_apply_produces_exact_target_tree_for_channel_and_playlist(self):
        with temp_download_root():
            channel = make_bridge_source()
            channel.make_directory()
            channel_media = Media.objects.create(
                key='chvid1', source=channel, metadata=metadata,
            )
            old_channel_video_path = download_dummy_file(channel_media)

            playlist = make_bridge_source(
                source_type=Val(YouTube_SourceType.PLAYLIST),
                key='PLabcdefghijklmnopqrstuv',
                name='acq-src-PLabcdefghijklmnopqrstuv',
                directory='acq-src-PLabcdefghijklmnopqrstuv',
            )
            playlist.make_directory()
            playlist_media = Media.objects.create(
                key='plvid1', source=playlist, metadata=metadata,
            )
            old_playlist_video_path = download_dummy_file(playlist_media)

            before_file_count = sum(
                1 for p in (channel.directory_path.parent).rglob('*') if p.is_file()
            )

            output = run_backfill('--all-bridge-sources', '--apply')
            self.assertIn('apply', output)

            channel.refresh_from_db()
            channel_media.refresh_from_db()
            playlist.refresh_from_db()
            playlist_media.refresh_from_db()

            # T3 profile applied.
            self.assertTrue(channel.write_nfo)
            self.assertTrue(channel.copy_channel_images)
            self.assertTrue(playlist.write_nfo)

            expected_video = channel.directory_path / 'Season 2017' / (
                's2017e091101 - no fancy stuff title [chvid1].mkv'
            )
            self.assertTrue(expected_video.exists())
            expected_nfo = channel.directory_path / 'Season 2017' / (
                's2017e091101 - no fancy stuff title [chvid1].nfo'
            )
            self.assertTrue(expected_nfo.exists())
            tree = ElementTree.fromstring(expected_nfo.read_text(encoding='utf-8'))
            self.assertEqual(tree.find('season').text, '2017')
            self.assertEqual(tree.find('episode').text, '91101')
            self.assertEqual(tree.find('showtitle').text, 'test uploader')

            channel_tvshow = channel.directory_path / 'tvshow.nfo'
            self.assertTrue(channel_tvshow.exists())
            ElementTree.fromstring(channel_tvshow.read_text(encoding='utf-8'))

            playlist_video = playlist.directory_path / 'Season 2017' / (
                's2017e091101 - no fancy stuff title [plvid1].mkv'
            )
            self.assertTrue(playlist_video.exists())
            playlist_nfo = playlist.directory_path / 'Season 2017' / (
                's2017e091101 - no fancy stuff title [plvid1].nfo'
            )
            self.assertTrue(playlist_nfo.exists())
            playlist_tree = ElementTree.fromstring(
                playlist_nfo.read_text(encoding='utf-8'),
            )
            # The playlist is now filed by the date scheme, so its NFO
            # numbering matches its filename, like a channel's.
            self.assertEqual(playlist_tree.find('season').text, '2017')
            self.assertEqual(playlist_tree.find('episode').text, '91101')

            # Old paths are gone (files were moved, not copied)...
            self.assertFalse(old_channel_video_path.exists())
            self.assertFalse(old_playlist_video_path.exists())
            # ...but nothing was deleted: +1 tvshow.nfo per source (x2),
            # +1 episode NFO per media (x2), relative to the two original
            # dummy video files, which still exist (just moved/renamed).
            after_file_count = sum(
                1 for p in (channel.directory_path.parent).rglob('*') if p.is_file()
            )
            self.assertEqual(after_file_count, before_file_count + 4)

    def test_nfo_is_written_even_when_no_rename_is_needed(self):
        '''
            N4: rename_files() only rewrites a media's NFO as a side
            effect of an actual move, and only when write_nfo AND
            copy_thumbnails are both on. Here the file already sits at
            the post-overlay path (nothing to rename), so this exercises
            this command's OWN independent NFO step, not rename_files()'s.
        '''
        with temp_download_root():
            source = make_bridge_source()
            source.make_directory()
            media = Media.objects.create(key='vid1', source=source, metadata=metadata)

            overlay = source_defaults()['channel']
            clone = copy.copy(source)
            for field, value in overlay.items():
                setattr(clone, field, value)
            media.source = clone
            new_path = media.filepath
            media.source = source  # restore for the save() below
            new_path.parent.mkdir(parents=True, exist_ok=True)
            new_path.write_bytes(b'already-at-new-path')
            media.media_file.name = str(
                new_path.relative_to(media_file_storage.location),
            )
            media.downloaded = True
            media.save()

            self.assertFalse(new_path.with_suffix('.nfo').exists())

            output = run_backfill('--source', str(source.uuid), '--apply')
            self.assertIn('already_in_place: 1', output)
            self.assertIn('nfo_written: 1', output)
            self.assertIn('nfo_unchanged: 0', output)
            self.assertTrue(new_path.with_suffix('.nfo').exists())

    def test_second_apply_is_a_no_op(self):
        with temp_download_root():
            source = make_bridge_source()
            source.make_directory()
            media = Media.objects.create(key='vid1', source=source, metadata=metadata)
            download_dummy_file(media)

            first_output = run_backfill('--source', str(source.uuid), '--apply')
            self.assertIn('renamed: 1', first_output)

            second_output = run_backfill('--source', str(source.uuid), '--apply')
            self.assertIn('renamed: 0', second_output)
            self.assertIn('already_in_place: 1', second_output)
            self.assertIn('nfo_written: 0', second_output)
            self.assertIn('nfo_unchanged: 1', second_output)
            self.assertIn('tvshow_written: 0', second_output)

    def test_non_bridge_sources_are_untouched_by_all_bridge_sources(self):
        with temp_download_root():
            normal_source = make_bridge_source(
                key='UCzzzzzzzzzzzzzzzzzzzzzz',
                name='My Regular Channel',
                directory='myregularchannel',
            )
            normal_source.make_directory()
            normal_media = Media.objects.create(
                key='regvid', source=normal_source, metadata=metadata,
            )
            download_dummy_file(normal_media)
            old_format = normal_source.media_format

            bridge_source = make_bridge_source()
            bridge_source.make_directory()
            bridge_media = Media.objects.create(
                key='bvid', source=bridge_source, metadata=metadata,
            )
            download_dummy_file(bridge_media)

            run_backfill('--all-bridge-sources', '--apply')

            normal_source.refresh_from_db()
            self.assertEqual(normal_source.media_format, old_format)
            self.assertFalse(normal_source.write_nfo)

            bridge_source.refresh_from_db()
            self.assertTrue(bridge_source.write_nfo)

    def test_invalid_source_defaults_env_raises_and_changes_nothing(self):
        with temp_download_root():
            source = make_bridge_source()
            source.make_directory()
            media = Media.objects.create(key='vid1', source=source, metadata=metadata)
            download_dummy_file(media)
            old_format = source.media_format

            with override_settings():
                with patch.dict(
                    'os.environ',
                    {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': '{not valid json'},
                ):
                    with self.assertRaises(CommandError):
                        run_backfill('--source', str(source.uuid), '--apply')

            source.refresh_from_db()
            self.assertEqual(source.media_format, old_format)
            self.assertFalse(source.write_nfo)

    def test_source_and_all_bridge_sources_are_mutually_exclusive(self):
        with temp_download_root():
            source = make_bridge_source()
            with self.assertRaises(CommandError):
                # argparse's mutually-exclusive-group error surfaces as a
                # SystemExit normally; call_command wraps parser errors as
                # CommandError for programmatic callers.
                run_backfill('--source', str(source.uuid), '--all-bridge-sources')


class BackfillFailureHandlingTestCase(TestCase):
    '''Partial failures, re-run safety and the run-as-owner guard.'''

    COMMAND = 'sync.management.commands.medianest_backfill_plex_sidecars'

    def setUp(self):
        logging.disable(logging.CRITICAL)
        _clear_show_title_cache()

    def make_downloaded(self, **source_overrides):
        source = make_bridge_source(**source_overrides)
        source.make_directory()
        media = Media.objects.create(key='vid1', source=source, metadata=metadata)
        old_path = download_dummy_file(media)
        return source, media, old_path

    def test_sidecar_failure_keeps_the_completed_rename(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            with (
                patch.object(
                    Media, 'thumb_file_exists',
                    new_callable=PropertyMock, return_value=True,
                ),
                patch.object(
                    Media, 'copy_thumbnail', side_effect=OSError('disk full'),
                ),
                self.assertRaises(CommandError),
            ):
                run_backfill('--source', str(source.uuid), '--apply')
            media.refresh_from_db()
            self.assertFalse(old_path.exists())
            self.assertTrue(media.media_file_exists)
            self.assertIn('Season 2017', media.media_file.path)

    def test_occupied_target_is_an_error_and_nothing_moves(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            target = source.directory_path / 'Season 2017' / (
                's2017e091101 - no fancy stuff title [vid1].mkv'
            )
            target.parent.mkdir(parents=True)
            target.write_bytes(b'someone else')
            with self.assertRaises(CommandError):
                run_backfill('--source', str(source.uuid), '--apply')
            self.assertTrue(old_path.exists())
            self.assertEqual(target.read_bytes(), b'someone else')
            self.assertEqual(list(target.parent.glob('*.nfo')), [])

    def test_dry_run_reports_the_occupied_target_too(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            target = source.directory_path / 'Season 2017' / (
                's2017e091101 - no fancy stuff title [vid1].mkv'
            )
            target.parent.mkdir(parents=True)
            target.write_bytes(b'someone else')
            with self.assertRaises(CommandError):
                run_backfill('--source', str(source.uuid))
            self.assertTrue(old_path.exists())

    def test_occupied_sidecar_target_is_an_error_and_nothing_moves(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            old_nfo = old_path.with_suffix('.nfo')
            old_nfo.write_text('<episodedetails/>', encoding='utf-8')
            target_nfo = source.directory_path / 'Season 2017' / (
                's2017e091101 - no fancy stuff title [vid1].nfo'
            )
            target_nfo.parent.mkdir(parents=True)
            target_nfo.write_text('someone else', encoding='utf-8')
            with self.assertRaises(CommandError):
                run_backfill('--source', str(source.uuid), '--apply')
            self.assertTrue(old_path.exists())
            self.assertTrue(old_nfo.exists())
            self.assertEqual(
                target_nfo.read_text(encoding='utf-8'), 'someone else',
            )

    def test_nfo_failure_inside_rename_keeps_the_moved_file_in_the_db(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            # Only rename_files()'s own NFO rewrite goes through this name.
            with (
                patch(
                    'sync.models.media.write_text_file',
                    side_effect=OSError('read-only'),
                ),
                self.assertRaises(CommandError),
            ):
                run_backfill('--source', str(source.uuid), '--apply')
            media.refresh_from_db()
            self.assertFalse(old_path.exists())
            self.assertTrue(media.media_file_exists)
            self.assertIn('Season 2017', media.media_file.path)

    def test_an_earlier_half_finished_move_is_adopted(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            target = source.directory_path / 'Season 2017' / (
                's2017e091101 - no fancy stuff title [vid1].mkv'
            )
            target.parent.mkdir(parents=True)
            old_path.rename(target)
            output = run_backfill('--source', str(source.uuid), '--apply')
            self.assertIn('adopted: 1', output)
            media.refresh_from_db()
            self.assertEqual(media.media_file.path, str(target))
            self.assertTrue(target.with_suffix('.nfo').exists())

    def test_second_row_cannot_adopt_a_target_the_first_row_just_claimed(self):
        '''
            Two rows resolving to the same target under a custom
            media_format: the first row's real move must claim that target
            for the rest of this run, so the second row (whose own current
            file happens to already be missing) is refused rather than
            silently adopting the first row's freshly-moved file.

            RENAME_ALL_SOURCES disabled: this scenario's second row is
            refused regardless (its own current file is missing), which
            would otherwise also trip the unrelated rename-cascade gate
            (CascadeGateTestCase) and block the source from being saved
            at all -- masking the actual per-media claim-tracking
            behavior this test exists to exercise.
        '''
        overlay = '{"*": {"media_format": "shared.{ext}"}}'
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source = make_bridge_source()
            source.make_directory()
            first = Media.objects.create(key='vid1', source=source, metadata=metadata)
            download_dummy_file(first)
            second = Media.objects.create(key='vid2', source=source, metadata=metadata)
            second_old_path = download_dummy_file(second)
            second_old_path.unlink()

            with self.assertRaises(CommandError):
                run_backfill('--source', str(source.uuid), '--apply')

            first.refresh_from_db()
            second.refresh_from_db()
            self.assertEqual(
                Path(first.media_file.path), source.directory_path / 'shared.mkv',
            )
            # The second row must not have been silently re-pointed at the
            # first row's file.
            self.assertEqual(
                str(second.media_file),
                str(second_old_path.relative_to(media_file_storage.location)),
            )

    def test_already_in_place_but_missing_video_is_an_error(self):
        with temp_download_root():
            source = make_bridge_source()
            source.make_directory()
            media = Media.objects.create(key='vid1', source=source, metadata=metadata)

            overlay = source_defaults()['channel']
            clone = copy.copy(source)
            for field, value in overlay.items():
                setattr(clone, field, value)
            media.source = clone
            new_path = media.filepath
            media.source = source
            media.media_file.name = str(
                new_path.relative_to(media_file_storage.location),
            )
            media.downloaded = True
            media.save()
            # No file actually written at new_path: the DB says it is
            # already at its target, but the video itself is gone.

            with self.assertRaises(CommandError):
                run_backfill('--source', str(source.uuid), '--apply')

    def test_stray_sidecar_outside_target_dir_is_reported_as_an_error(self):
        '''
            A prior run's rename_files() moved the video and saved
            media_file, then raised partway through moving an old-name
            sidecar (subtitle, JSON, ...). The next run's already-in-place
            branch must not silently exit clean while that sidecar is
            still orphaned under the old name.
        '''
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            target = source.directory_path / 'Season 2017' / (
                's2017e091101 - no fancy stuff title [vid1].mkv'
            )
            target.parent.mkdir(parents=True)
            old_path.rename(target)
            media.media_file.name = str(target.relative_to(media_file_storage.location))
            media.save()
            stray = old_path.with_suffix('.en.srt')
            stray.write_text('left behind', encoding='utf-8')

            with self.assertRaises(CommandError):
                run_backfill('--source', str(source.uuid), '--apply')
            # Never moved or deleted, just reported.
            self.assertTrue(stray.exists())

    def test_another_video_sharing_the_stem_prefix_is_not_moved(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            other_path = old_path.with_name(old_path.stem + 'x.mkv')
            other_path.write_bytes(b'another video')
            other = Media.objects.create(
                key='vid2', source=source, metadata=metadata, downloaded=True,
            )
            other.media_file.name = str(
                other_path.relative_to(media_file_storage.location)
            )
            other.save()
            with self.assertRaises(CommandError):
                run_backfill('--source', str(source.uuid), '--apply')
            self.assertTrue(old_path.exists())
            media.refresh_from_db()
            self.assertEqual(media.media_file.path, str(old_path))

    def test_downloaded_row_without_a_file_is_an_error(self):
        '''
            RENAME_ALL_SOURCES disabled: the one downloaded row here has
            no media_file at all, always refused -- with the cascade
            enabled (the default) that would also trip the rename-cascade
            gate (CascadeGateTestCase) and skip the WHOLE source,
            including its tvshow.nfo write, which is not what this test
            means to exercise (that a media-level failure does not stop
            the source-level tvshow.nfo write).
        '''
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source = make_bridge_source()
            source.make_directory()
            Media.objects.create(
                key='vid1', source=source, metadata=metadata, downloaded=True,
            )
            with self.assertRaises(CommandError):
                run_backfill('--source', str(source.uuid), '--apply')
            self.assertEqual(list(source.directory_path.rglob('*.nfo')), [
                source.directory_path / 'tvshow.nfo',
            ])

    def test_tvshow_nfo_write_failure_is_counted(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            with (
                patch(
                    'sync.tvshow_nfo.write_text_file',
                    side_effect=OSError('read-only'),
                ),
                self.assertRaises(CommandError),
            ):
                run_backfill('--source', str(source.uuid), '--apply')
            self.assertFalse((source.directory_path / 'tvshow.nfo').exists())

    def test_locked_media_fail_the_run_so_it_is_repeated(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            with (
                patch(
                    f'{self.COMMAND}.huey_lock_task',
                    side_effect=TaskLockedException('busy'),
                ),
                self.assertRaises(CommandError) as ctx,
            ):
                run_backfill('--source', str(source.uuid), '--apply')
            self.assertIn('1 locked', str(ctx.exception))
            self.assertTrue(old_path.exists())

    def test_one_failing_source_does_not_stop_the_others(self):
        with temp_download_root():
            first, _, _ = self.make_downloaded()
            second = make_bridge_source(
                key='UCzyxwvutsrqponmlkjihgfe',
                name='acq-src-UCzyxwvutsrqponmlkjihgfe',
                directory='acq-src-UCzyxwvutsrqponmlkjihgfe',
            )
            second.make_directory()
            real_save = BackfillCommand._save_overlay
            calls = []

            def save_once_then_fail(command, source, changes):
                calls.append(source.pk)
                if len(calls) == 1:
                    raise RuntimeError('db down')
                return real_save(command, source, changes)

            with (
                patch.object(BackfillCommand, '_save_overlay', save_once_then_fail),
                self.assertRaises(CommandError),
            ):
                run_backfill('--all-bridge-sources', '--apply')
            self.assertEqual(len(calls), 2)

    def test_apply_refuses_to_run_as_a_different_user(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            real_uid = os.geteuid()
            with (
                patch(f'{self.COMMAND}.os.geteuid', return_value=real_uid + 1),
                self.assertRaises(CommandError) as ctx,
            ):
                run_backfill('--source', str(source.uuid), '--apply')
            self.assertIn('docker exec -u app', str(ctx.exception))
            self.assertTrue(old_path.exists())
            source.refresh_from_db()
            self.assertFalse(source.write_nfo)

    def test_multi_value_sponsorblock_categories_survive_the_overlay(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded(
                sponsorblock_categories='sponsor,selfpromo',
            )
            run_backfill('--source', str(source.uuid), '--apply')
            source.refresh_from_db()
            self.assertTrue(source.write_nfo)
            self.assertEqual(
                sorted(source.sponsorblock_categories.selected_choices),
                ['selfpromo', 'sponsor'],
            )

    def test_second_apply_does_not_save_the_source_again(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            run_backfill('--source', str(source.uuid), '--apply')
            with patch.object(Source, 'save') as mock_save:
                run_backfill('--source', str(source.uuid), '--apply')
            mock_save.assert_not_called()

    def test_configured_list_field_does_not_resave_the_source(self):
        overlay = (
            '{"*": {"write_nfo": true, "copy_thumbnails": true, '
            '"copy_channel_images": true, "index_streams": false, '
            '"sponsorblock_categories": ["sponsor", "selfpromo"]}}'
        )
        with (
            temp_download_root(),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source, media, old_path = self.make_downloaded()
            run_backfill('--source', str(source.uuid), '--apply')
            source.refresh_from_db()
            self.assertEqual(
                sorted(source.sponsorblock_categories.selected_choices),
                ['selfpromo', 'sponsor'],
            )
            with patch.object(Source, 'save') as mock_save:
                run_backfill('--source', str(source.uuid), '--apply')
            mock_save.assert_not_called()

    def test_a_value_the_form_normalizes_does_not_resave_the_source(self):
        overlay = (
            '{"*": {"write_nfo": true, "index_schedule": "3600", '
            '"media_format": " {key}.{ext} "}}'
        )
        with (
            temp_download_root(),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source, media, old_path = self.make_downloaded()
            run_backfill('--source', str(source.uuid), '--apply')
            source.refresh_from_db()
            self.assertEqual(source.media_format, '{key}.{ext}')
            with patch.object(Source, 'save') as mock_save:
                run_backfill('--source', str(source.uuid), '--apply')
            mock_save.assert_not_called()

    def test_existing_local_thumbnail_is_copied(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            with (
                patch.object(
                    Media, 'thumb_file_exists',
                    new_callable=PropertyMock, return_value=True,
                ),
                patch.object(Media, 'copy_thumbnail') as mock_copy,
            ):
                output = run_backfill('--source', str(source.uuid), '--apply')
            mock_copy.assert_called_once()
            self.assertIn('thumbs_copied: 1', output)

    def test_images_enqueued_counts_the_signal_triggered_job_without_a_duplicate(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            # copy_channel_images starts False; the built-in profile turns
            # it on, so source_pre_save's own check enqueues the image job
            # itself once apply saves the source -- this command must not
            # also enqueue it directly, but must still count it (dry-run
            # predicts the same signal-triggered job would be enqueued).
            dry = run_backfill('--source', str(source.uuid))
            self.assertIn('images_enqueued: 1', dry)

            with patch(f'{self.COMMAND}.download_source_images') as mock_enqueue:
                applied = run_backfill('--source', str(source.uuid), '--apply')
            mock_enqueue.assert_not_called()
            self.assertIn('images_enqueued: 1', applied)

    def test_dry_run_predicts_the_apply_counts(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            dry = run_backfill('--source', str(source.uuid))
            applied = run_backfill('--source', str(source.uuid), '--apply')
            # nfo_written is left out: in apply, rename_files() rewrites the
            # moved NFO itself (write_nfo and copy_thumbnails are on), so
            # the command then finds it unchanged.
            for field in ('renamed', 'tvshow_written', 'errors'):
                line = next(
                    x for x in dry.splitlines() if x.strip().startswith(f'{field}:')
                )
                self.assertIn(line.strip(), applied)

    def test_target_side_nfo_not_covered_by_a_move_is_occupied(self):
        '''
            No .nfo beside the OLD video (nothing for rename_files()'s own
            sidecar-move glob to find), but the TARGET directory already
            has one from something unrelated -- this command's own
            _handle_episode_nfo() would otherwise silently clobber it
            right after the video moves. Must be refused up front instead.
        '''
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            target = source.directory_path / 'Season 2017' / (
                's2017e091101 - no fancy stuff title [vid1].mkv'
            )
            target_nfo = target.with_suffix('.nfo')
            target_nfo.parent.mkdir(parents=True)
            target_nfo.write_text('foreign nfo', encoding='utf-8')
            with self.assertRaises(CommandError):
                run_backfill('--source', str(source.uuid), '--apply')
            self.assertTrue(old_path.exists())
            self.assertFalse(target.exists())
            self.assertEqual(
                target_nfo.read_text(encoding='utf-8'), 'foreign nfo',
            )

    def test_target_side_thumbnail_not_covered_by_a_move_is_left_alone(self):
        '''
            _handle_thumbnail() never overwrites an existing .jpg, so a
            foreign one at the target name does not block the rename.
        '''
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            target = source.directory_path / 'Season 2017' / (
                's2017e091101 - no fancy stuff title [vid1].mkv'
            )
            target_jpg = target.with_suffix('.jpg')
            target_jpg.parent.mkdir(parents=True)
            target_jpg.write_bytes(b'foreign thumbnail')
            with patch.object(
                Media, 'thumb_file_exists',
                new_callable=PropertyMock, return_value=True,
            ):
                output = run_backfill('--source', str(source.uuid), '--apply')
            self.assertIn('thumbs_copied: 0', output)
            self.assertFalse(old_path.exists())
            self.assertTrue(target.exists())
            self.assertEqual(target_jpg.read_bytes(), b'foreign thumbnail')

    def test_adoption_with_leftover_sidecar_is_an_error_and_nothing_is_adopted(self):
        '''
            The video already sits at its target (an earlier run moved it)
            but the DB row still points at the old (now-gone) path --
            normally "adopted". Here that earlier run also left a stray
            same-key sidecar behind under the old name/location; adoption
            must refuse instead of silently pointing the row at `target`
            while the leftover sits forgotten.
        '''
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            target = source.directory_path / 'Season 2017' / (
                's2017e091101 - no fancy stuff title [vid1].mkv'
            )
            target.parent.mkdir(parents=True)
            old_path.rename(target)
            stray = old_path.with_suffix('.en.srt')
            stray.write_text('left behind', encoding='utf-8')

            with self.assertRaises(CommandError):
                run_backfill('--source', str(source.uuid), '--apply')
            media.refresh_from_db()
            self.assertEqual(
                str(media.media_file),
                str(old_path.relative_to(media_file_storage.location)),
            )
            self.assertTrue(stray.exists())
            self.assertTrue(target.exists())

    def test_stray_snapshot_is_built_once_per_source(self):
        '''
            Two already-in-place media of the SAME source both need a
            stray-sidecar check; the directory should be walked (rglob)
            once for the whole source, not once per media.
        '''
        with temp_download_root():
            source = make_bridge_source()
            source.make_directory()
            for key in ('vid1', 'vid2'):
                media = Media.objects.create(
                    key=key, source=source, metadata=metadata,
                )
                overlay = source_defaults()['channel']
                clone = copy.copy(source)
                for field, value in overlay.items():
                    setattr(clone, field, value)
                media.source = clone
                new_path = media.filepath
                media.source = source
                new_path.parent.mkdir(parents=True, exist_ok=True)
                new_path.write_bytes(b'already-at-new-path')
                media.media_file.name = str(
                    new_path.relative_to(media_file_storage.location),
                )
                media.downloaded = True
                media.save()

            original_rglob = Path.rglob
            calls = []

            def counting_rglob(path_self, *args, **kwargs):
                calls.append((path_self, args, kwargs))
                return original_rglob(path_self, *args, **kwargs)

            with patch.object(Path, 'rglob', counting_rglob):
                output = run_backfill('--source', str(source.uuid), '--apply')
            self.assertIn('already_in_place: 2', output)
            self.assertEqual(len(calls), 1)

    def test_image_enqueue_uses_task_history_schedule_with_remove_duplicates(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded(
                copy_channel_images=True,
            )
            with patch(f'{self.COMMAND}.TaskHistory') as mock_th:
                output = run_backfill('--source', str(source.uuid), '--apply')
            mock_th.schedule.assert_called_once()
            args, kwargs = mock_th.schedule.call_args
            self.assertEqual(args[0], download_source_images)
            self.assertEqual(args[1], str(source.pk))
            self.assertTrue(kwargs.get('remove_duplicates'))
            self.assertIn('images_enqueued: 1', output)

    def test_re_run_reschedules_via_task_history_not_a_raw_duplicate_call(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded(
                copy_channel_images=True,
            )
            with patch(f'{self.COMMAND}.TaskHistory') as mock_th:
                run_backfill('--source', str(source.uuid), '--apply')
                run_backfill('--source', str(source.uuid), '--apply')
            # poster.jpg is never actually created by these tests (the
            # async job never runs without a huey consumer), so every
            # re-run still schedules -- but always through
            # TaskHistory.schedule(remove_duplicates=True), which lets
            # huey's own on_executing_remove_duplicates() revoke whichever
            # earlier pending job loses the race once a worker executes.
            self.assertEqual(mock_th.schedule.call_count, 2)
            for _, kwargs in mock_th.schedule.call_args_list:
                self.assertTrue(kwargs.get('remove_duplicates'))

    def test_image_enqueue_skipped_when_poster_already_exists(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded(
                copy_channel_images=True,
            )
            (source.directory_path / 'poster.jpg').write_bytes(b'existing poster')
            with patch(f'{self.COMMAND}.TaskHistory') as mock_th:
                output = run_backfill('--source', str(source.uuid), '--apply')
            mock_th.schedule.assert_not_called()
            self.assertIn('images_enqueued: 0', output)

    def test_locked_media_prints_a_stdout_line(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            with patch(
                f'{self.COMMAND}.huey_lock_task',
                side_effect=TaskLockedException('busy'),
            ):
                output, exc = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            self.assertIsNotNone(exc)
            self.assertIn('LOCKED', output)
            self.assertIn(str(media), output)

    def test_rename_problem_prints_a_stdout_line(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            target = source.directory_path / 'Season 2017' / (
                's2017e091101 - no fancy stuff title [vid1].mkv'
            )
            target.parent.mkdir(parents=True)
            target.write_bytes(b'someone else')
            output, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            self.assertIsNotNone(exc)
            self.assertIn('FAILED', output)
            self.assertIn(str(media), output)
            self.assertIn('already occupied', output)

    def test_apply_tvshow_write_failure_prints_a_stdout_line(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            with patch(
                'sync.tvshow_nfo.write_text_file',
                side_effect=OSError('read-only'),
            ):
                output, exc = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            self.assertIsNotNone(exc)
            self.assertIn('FAILED', output)
            self.assertIn('tvshow.nfo', output)

    def test_handle_based_channel_source_has_no_profile_and_is_skipped(self):
        with temp_download_root():
            source = make_bridge_source(
                source_type=Val(YouTube_SourceType.CHANNEL),
                key='@somehandle',
                name='acq-src-somehandle',
                directory='acq-src-somehandle',
            )
            source.make_directory()
            with self.assertRaises(CommandError) as ctx:
                run_backfill('--source', str(source.uuid), '--apply')
            self.assertIn('1 error', str(ctx.exception))
            source.refresh_from_db()
            self.assertFalse(source.write_nfo)

    def test_one_sources_invalid_overlay_does_not_stop_processing_others(self):
        '''
            A per-source overlay form-validation failure (SourceForm plus
            run_edit_source_checks(), applied against THIS source's own
            current field values -- see _overlay_form()) must skip only
            that source, not abort the rest of an --all-bridge-sources
            run. Forces the failure for one specific source via a patched
            run_edit_source_checks() rather than crafting a real invalid
            value, so this test cannot itself create anything outside the
            sandboxed temp download root.
        '''
        with temp_download_root():
            channel = make_bridge_source()
            channel.make_directory()
            Media.objects.create(key='chvid1', source=channel, metadata=metadata)
            old_format = channel.media_format

            playlist = make_bridge_source(
                source_type=Val(YouTube_SourceType.PLAYLIST),
                key='PLabcdefghijklmnopqrstuv',
                name='acq-src-PLabcdefghijklmnopqrstuv',
                directory='acq-src-PLabcdefghijklmnopqrstuv',
            )
            playlist.make_directory()

            real_checks = medianest_source_forms.run_edit_source_checks

            def fail_only_for_channel(form):
                real_checks(form)
                if form.instance.pk == channel.pk:
                    form.add_error(
                        'media_format', ValidationError('forced failure'),
                    )

            with patch(
                f'{self.COMMAND}.run_edit_source_checks',
                side_effect=fail_only_for_channel,
            ):
                output, exc = run_backfill_capture(
                    '--all-bridge-sources', '--apply',
                )

            self.assertIsNotNone(exc)
            self.assertIn('SKIPPED', output)
            # The playlist source is still visited and reported normally
            # -- the channel source's own validation failure does not
            # abort the rest of the --all-bridge-sources run.
            self.assertIn(playlist.name, output)
            channel.refresh_from_db()
            self.assertEqual(channel.media_format, old_format)
            playlist.refresh_from_db()
            self.assertTrue(playlist.write_nfo)


class CascadeGateTestCase(TestCase):
    '''
        T4 rename-cascade gate: saving a source whose overlay changes a
        field fires source_post_save -> save_all_media_for_source ->
        rename_all_media_for_source (sync/tasks.py), which has none of
        this command's own refusal checks and would silently overwrite a
        same-stem sidecar via Path.replace(). --apply must not save such a
        source when any of its media would be refused AND the cascade is
        enabled for it (settings.RENAME_ALL_SOURCES / RENAME_SOURCES,
        mirroring rename_all_media_for_source's own gate exactly).
    '''

    def setUp(self):
        logging.disable(logging.CRITICAL)
        _clear_show_title_cache()

    def make_conflicted_source(self, **source_overrides):
        '''
            A bridge source with one downloaded media whose rename target
            is already occupied by someone else's file -- always refused,
            regardless of any cascade setting.
        '''
        source = make_bridge_source(**source_overrides)
        source.make_directory()
        media = Media.objects.create(key='vid1', source=source, metadata=metadata)
        old_path = download_dummy_file(media)
        target = source.directory_path / 'Season 2017' / (
            's2017e091101 - no fancy stuff title [vid1].mkv'
        )
        target.parent.mkdir(parents=True)
        target.write_bytes(b'someone else')
        return source, media, old_path, target

    def test_apply_does_not_save_when_cascade_enabled_and_media_refused(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=True, RENAME_SOURCES=[]),
        ):
            source, media, old_path, target = self.make_conflicted_source()
            output, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            self.assertIsNotNone(exc)
            self.assertIn('errors: 1', output)
            self.assertIn('SKIPPED', output)
            self.assertIn(
                '1 already-downloaded media item(s) would be refused', output,
            )
            self.assertIn('TUBESYNC_RENAME_ALL_SOURCES=false', output)
            source.refresh_from_db()
            self.assertFalse(source.write_nfo)  # overlay never saved
            self.assertTrue(old_path.exists())
            self.assertEqual(target.read_bytes(), b'someone else')

    def test_apply_proceeds_when_cascade_disabled(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, media, old_path, target = self.make_conflicted_source()
            output, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            # The command still exits non-zero (the one conflicted media
            # is still refused, for its own unrelated reason), but the
            # gate itself did not block the save this time.
            self.assertIsNotNone(exc)
            self.assertNotIn(
                'already-downloaded media item(s) would be refused', output,
            )
            source.refresh_from_db()
            self.assertTrue(source.write_nfo)  # overlay WAS saved

    def test_apply_proceeds_when_cascade_enabled_but_nothing_is_refused(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=True, RENAME_SOURCES=[]),
        ):
            source = make_bridge_source()
            source.make_directory()
            media = Media.objects.create(key='vid1', source=source, metadata=metadata)
            download_dummy_file(media)

            output = run_backfill('--source', str(source.uuid), '--apply')
            self.assertIn('renamed: 1', output)
            self.assertIn('errors: 0', output)
            source.refresh_from_db()
            self.assertTrue(source.write_nfo)

    def test_apply_gate_triggers_via_rename_sources_list(self):
        with (
            temp_download_root(),
            override_settings(
                RENAME_ALL_SOURCES=False,
                RENAME_SOURCES=['acq-src-UCabcdefghijklmnopqrstuv'],
            ),
        ):
            source, media, old_path, target = self.make_conflicted_source()
            output, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            self.assertIsNotNone(exc)
            self.assertIn(
                '1 already-downloaded media item(s) would be refused', output,
            )
            source.refresh_from_db()
            self.assertFalse(source.write_nfo)

    def test_dry_run_reports_apply_would_skip_the_source(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=True, RENAME_SOURCES=[]),
        ):
            source, media, old_path, target = self.make_conflicted_source()
            output, exc = run_backfill_capture('--source', str(source.uuid))
            # Dry-run stops the source where --apply would, so its summary
            # matches the apply run's.
            self.assertIsNotNone(exc)
            self.assertIn('NOTE', output)
            self.assertIn(
                '1 already-downloaded media item(s) would be refused', output,
            )
            self.assertIn('media_seen: 0', output)
            self.assertIn('errors: 1', output)
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            self.assertIsNotNone(exc)
            self.assertEqual(summary_of(output), summary_of(applied))


class BackfillFollowUpMixin:

    COMMAND = 'sync.management.commands.medianest_backfill_plex_sidecars'
    TARGET_NAME = 's2017e091101 - no fancy stuff title [vid1]'

    def setUp(self):
        logging.disable(logging.CRITICAL)
        _clear_show_title_cache()

    def make_downloaded(self, key='vid1', source=None):
        if source is None:
            source = make_bridge_source()
            source.make_directory()
        media = Media.objects.create(key=key, source=source, metadata=metadata)
        old_path = download_dummy_file(media)
        return source, media, old_path

    def target_dir(self, source):
        return source.directory_path / 'Season 2017'


class BackfillReviewFollowUpTestCase(BackfillFollowUpMixin, TestCase):
    '''
        Review follow-up: rename_files()'s key sweep, projected targets,
        adoption safety, targeted source saves, downloads that finish or
        are still running during a run, and dry-run side effects.
    '''

    def test_an_orphan_with_the_key_is_listed_and_then_moved(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            orphan = source.directory_path / 'leftovers' / 'old name [vid1].en.srt'
            orphan.parent.mkdir()
            orphan.write_bytes(b'subtitle')
            destination = self.target_dir(source) / f'{self.TARGET_NAME}.en.srt'

            dry = run_backfill('--source', str(source.uuid))
            self.assertIn('key_matched_moves: 1', dry)
            self.assertIn(f'key match {orphan} -> {destination}', dry)
            self.assertTrue(orphan.exists())

            applied = run_backfill('--source', str(source.uuid), '--apply')
            self.assertIn('key_matched_moves: 1', applied)
            self.assertFalse(orphan.exists())
            self.assertEqual(destination.read_bytes(), b'subtitle')

    def test_another_medias_sidecar_with_the_key_is_refused(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, media, old_path = self.make_downloaded()
            _, other, other_path = self.make_downloaded(key='other', source=source)
            other_sidecar = other_path.with_name(other_path.stem + '.vid1.txt')
            other_sidecar.write_bytes(b'notes')

            dry, exc = run_backfill_capture('--source', str(source.uuid))
            self.assertIsNotNone(exc)
            self.assertIn('other media files would be moved with it', dry)

            with self.assertRaises(CommandError):
                run_backfill('--source', str(source.uuid), '--apply')
            self.assertTrue(old_path.exists())
            # `other` (processed first) took its own sidecar along; vid1's
            # key sweep then refused to take it from there.
            other.refresh_from_db()
            other_video = Path(other.media_file.path)
            self.assertIn('Season 2017', str(other_video))
            moved_sidecar = other_video.with_name(other_video.stem + '.vid1.txt')
            self.assertEqual(moved_sidecar.read_bytes(), b'notes')

    def test_a_directory_matching_the_key_is_refused(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, media, old_path = self.make_downloaded()
            (source.directory_path / 'vid1 extras').mkdir()
            with self.assertRaises(CommandError):
                run_backfill('--source', str(source.uuid), '--apply')
            self.assertTrue(old_path.exists())

    def test_dry_run_refuses_a_target_an_earlier_row_projects(self):
        overlay = '{"*": {"media_format": "shared.{ext}"}}'
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source, _, _ = self.make_downloaded()
            self.make_downloaded(key='vid2', source=source)
            dry, exc = run_backfill_capture('--source', str(source.uuid))
            self.assertIsNotNone(exc)
            self.assertIn('renamed: 1', dry)
            self.assertIn('errors: 1', dry)
            self.assertIn('is already occupied', dry)

    def test_a_symlinked_target_is_not_adopted(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            target = self.target_dir(source) / f'{self.TARGET_NAME}.mkv'
            target.parent.mkdir(parents=True)
            elsewhere = source.directory_path / 'elsewhere.mkv'
            old_path.rename(elsewhere)
            target.symlink_to(elsewhere)
            output, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            self.assertIsNotNone(exc)
            self.assertIn('it is a symlink', output)
            media.refresh_from_db()
            self.assertEqual(Path(media.media_file.path), old_path)

    def test_a_target_resolving_outside_the_download_root_is_not_adopted(self):
        with temp_download_root(), tempfile.TemporaryDirectory() as outside:
            source, media, old_path = self.make_downloaded()
            outside_season = Path(outside) / 'Season 2017'
            outside_season.mkdir()
            (outside_season / f'{self.TARGET_NAME}.mkv').write_bytes(b'x')
            self.target_dir(source).symlink_to(outside_season)
            old_path.unlink()
            output, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            self.assertIsNotNone(exc)
            self.assertIn('resolves outside', output)
            media.refresh_from_db()
            self.assertEqual(Path(media.media_file.path), old_path)

    def test_the_overlay_save_keeps_concurrent_edits(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            Source.objects.filter(pk=source.pk).update(filter_text='  spaced  ')
            schedule = source.target_schedule
            real_describe = BackfillCommand._describe_overlay_diff

            def edit_while_running(command, original, changes):
                # Another writer changes the source after the run read it.
                Source.objects.filter(pk=source.pk).update(days_to_keep=99)
                return real_describe(command, original, changes)

            with patch.object(
                BackfillCommand, '_describe_overlay_diff', edit_while_running,
            ):
                run_backfill('--source', str(source.uuid), '--apply')
            source.refresh_from_db()
            self.assertTrue(source.write_nfo)
            self.assertEqual(source.days_to_keep, 99)
            self.assertEqual(source.filter_text, '  spaced  ')
            self.assertEqual(source.target_schedule, schedule)

    def test_a_download_finishing_during_the_run_is_processed(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            late = Media.objects.create(key='late1', source=source, metadata=metadata)
            real_preflight = BackfillCommand._count_refused_media

            def finish_a_download(command, downloaded, media_files):
                # After the run read the downloaded media (the cascade
                # gate's preflight runs right after that).
                download_dummy_file(late)
                return real_preflight(command, downloaded, media_files)

            with patch.object(
                BackfillCommand, '_count_refused_media', finish_a_download,
            ):
                output = run_backfill('--source', str(source.uuid), '--apply')
            self.assertIn('finished downloading during this run', output)
            self.assertIn('renamed: 2', output)
            late.refresh_from_db()
            self.assertIn('Season 2017', late.media_file.path)

    def test_a_download_still_running_fails_the_run(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, media, old_path = self.make_downloaded()
            busy = Media.objects.create(key='busy1', source=source, metadata=metadata)
            Media.objects.filter(pk=busy.pk).update(skip=False, manual_skip=False)
            lock = huey_lock_task(f'media:{busy.uuid}', queue=Val(TaskQueue.DB))
            lock.acquire()
            try:
                output, exc = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            finally:
                lock.release()
            self.assertIsNotNone(exc)
            self.assertIn('1 in-flight', str(exc))
            self.assertIn('in_flight: 1', output)
            self.assertIn(f'IN FLIGHT: {busy}', output)

    @override_settings(SHRINK_OLD_MEDIA_METADATA=True)
    def test_dry_run_does_not_shrink_metadata(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            before = Media.objects.get(pk=media.pk).metadata
            with patch.object(Media, 'ingest_metadata') as ingest:
                run_backfill('--source', str(source.uuid))
            ingest.assert_not_called()
            self.assertEqual(Media.objects.get(pk=media.pk).metadata, before)

    def test_apply_counts_the_nfo_rename_files_wrote(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            dry = run_backfill('--source', str(source.uuid))
            applied = run_backfill('--source', str(source.uuid), '--apply')
            for output in (dry, applied):
                self.assertIn('nfo_written: 1', output)
                self.assertIn('nfo_unchanged: 0', output)

    def test_no_matching_sources(self):
        output = run_backfill('--all-bridge-sources')
        self.assertIn('No matching sources found.', output)

    def test_bad_or_unknown_source_uuid(self):
        with self.assertRaisesMessage(CommandError, 'Not a valid source UUID'):
            run_backfill('--source', 'not-a-uuid')
        with self.assertRaisesMessage(CommandError, 'No such source'):
            run_backfill('--source', '00000000-0000-0000-0000-000000000000')

    def test_a_rename_that_did_not_move_the_file_is_an_error(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            with patch.object(Media, 'rename_files'):
                output, exc = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            self.assertIsNotNone(exc)
            self.assertIn('did not happen', output)
            self.assertTrue(old_path.exists())

    def test_a_second_apply_does_not_copy_the_thumbnail_again(self):
        def fake_copy(media):
            media.thumbpath.write_bytes(b'thumb')

        with (
            temp_download_root(),
            patch.object(
                Media, 'thumb_file_exists',
                new_callable=PropertyMock, return_value=True,
            ),
            patch.object(Media, 'copy_thumbnail', autospec=True, side_effect=fake_copy),
        ):
            source, media, old_path = self.make_downloaded()
            first = run_backfill('--source', str(source.uuid), '--apply')
            second = run_backfill('--source', str(source.uuid), '--apply')
        self.assertIn('thumbs_copied: 1', first)
        self.assertIn('thumbs_copied: 0', second)

    def test_the_lock_error_is_shown(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            with patch(
                f'{self.COMMAND}.huey_lock_task',
                side_effect=TaskLockedException('unable to acquire lock media:x'),
            ):
                output, exc = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            self.assertIn('unable to acquire lock media:x', output)


class BackfillReviewFollowUp3TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Third review pass: foreign episode NFOs, symlinked paths,
        directories and destination collisions in the move sets, the
        in-flight scope, and the channel-image job.
    '''

    def target_nfo(self, source):
        return self.target_dir(source) / f'{self.TARGET_NAME}.nfo'

    def test_a_foreign_nfo_is_kept_for_an_in_place_media(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            run_backfill('--source', str(source.uuid), '--apply')
            nfo = self.target_nfo(source)
            foreign = '<episodedetails><title>Mine</title></episodedetails>'
            nfo.write_text(foreign)
            output, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            self.assertIsNotNone(exc)
            self.assertIn("it is not this media's episode NFO", output)
            self.assertEqual(nfo.read_text(), foreign)

    def test_this_medias_own_stale_nfo_is_rewritten_in_place(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            run_backfill('--source', str(source.uuid), '--apply')
            nfo = self.target_nfo(source)
            nfo.write_text(
                '<episodedetails><title>Stale</title><id>vid1</id></episodedetails>'
            )
            output = run_backfill('--source', str(source.uuid), '--apply')
            self.assertIn('nfo_written: 1', output)
            self.assertNotIn('Stale', nfo.read_text())

    def test_a_foreign_nfo_is_kept_when_adopting(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            target = self.target_dir(source) / f'{self.TARGET_NAME}.mkv'
            target.parent.mkdir(parents=True)
            old_path.rename(target)
            foreign = '<episodedetails><title>Mine</title></episodedetails>'
            self.target_nfo(source).write_text(foreign)
            output, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            self.assertIsNotNone(exc)
            self.assertIn('adopted: 1', output)
            self.assertEqual(self.target_nfo(source).read_text(), foreign)

    def test_this_medias_own_nfo_does_not_block_a_rename(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            nfo = self.target_nfo(source)
            nfo.parent.mkdir(parents=True)
            nfo.write_text('<episodedetails><uniqueid>vid1</uniqueid></episodedetails>')
            output = run_backfill('--source', str(source.uuid), '--apply')
            self.assertIn('renamed: 1', output)
            self.assertIn('<episode>', nfo.read_text())

    def test_a_symlinked_current_file_is_refused(self):
        with temp_download_root(), tempfile.TemporaryDirectory() as outside:
            source, media, old_path = self.make_downloaded()
            real = Path(outside) / 'real.mkv'
            real.write_bytes(b'outside')
            old_path.unlink()
            old_path.symlink_to(real)
            output, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            self.assertIsNotNone(exc)
            self.assertIn('is a symlink', output)
            self.assertTrue(old_path.is_symlink())
            self.assertEqual(real.read_bytes(), b'outside')

    def test_a_target_directory_outside_the_root_is_refused(self):
        with temp_download_root(), tempfile.TemporaryDirectory() as outside:
            source, media, old_path = self.make_downloaded()
            self.target_dir(source).symlink_to(outside)
            output, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            self.assertIsNotNone(exc)
            self.assertIn('resolves outside', output)
            self.assertTrue(old_path.exists())
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_a_directory_sharing_the_old_stem_is_refused(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, media, old_path = self.make_downloaded()
            extras = old_path.with_name(old_path.stem + '.extras')
            extras.mkdir()
            output, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            self.assertIsNotNone(exc)
            self.assertIn('directories would be moved with it', output)
            self.assertTrue(old_path.exists())
            self.assertTrue(extras.is_dir())

    def test_a_key_match_with_a_taken_destination_is_refused(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, media, old_path = self.make_downloaded()
            # The stem pass moves this one to the target .nfo name ...
            old_path.with_suffix('.nfo').write_text('<episodedetails><id>vid1</id></episodedetails>')
            # ... so this key match, which maps to the same name, would
            # be left behind.
            orphan = source.directory_path / 'leftovers' / 'old [vid1].nfo'
            orphan.parent.mkdir()
            orphan.write_text('orphan')
            output, exc = run_backfill_capture('--source', str(source.uuid))
            self.assertIsNotNone(exc)
            self.assertIn('destination is already taken', output)
            self.assertIn(str(orphan), output)

    def test_in_flight_is_only_counted_for_a_path_changing_overlay(self):
        overlay = '{"*": {"days_to_keep": 30}}'
        with (
            temp_download_root(),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source, media, old_path = self.make_downloaded()
            busy = Media.objects.create(key='busy1', source=source, metadata=metadata)
            Media.objects.filter(pk=busy.pk).update(skip=False, manual_skip=False)
            lock = huey_lock_task(f'media:{busy.uuid}', queue=Val(TaskQueue.DB))
            lock.acquire()
            try:
                output = run_backfill('--source', str(source.uuid), '--apply')
            finally:
                lock.release()
            self.assertIn('days_to_keep: 14 -> 30', output)
            self.assertIn('in_flight: 0', output)

    def test_turning_channel_images_on_with_a_poster_counts_the_job(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            (source.directory_path / 'poster.jpg').write_bytes(b'poster')
            for args in ((), ('--apply',)):
                with self.subTest(args=args):
                    Source.objects.filter(pk=source.pk).update(
                        copy_channel_images=False,
                    )
                    output = run_backfill('--source', str(source.uuid), *args)
                    self.assertIn('images_enqueued: 1', output)
                    self.assertIn('replaces the existing', output)

    @override_settings(SHRINK_OLD_MEDIA_METADATA=True)
    def test_dry_run_restores_the_shrink_setting(self):
        from django.conf import settings as live_settings
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            run_backfill('--source', str(source.uuid))
        self.assertTrue(live_settings.SHRINK_OLD_MEDIA_METADATA)

    def test_a_directory_sharing_the_old_stem_is_refused_without_key(self):
        overlay = (
            '{"*": {"media_format": '
            '"Season {episode_yyyy}/s{episode_yyyy}e{episode_mmddnn}.{ext}"}}'
        )
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source, media, old_path = self.make_downloaded()
            extras = old_path.with_name(old_path.stem + '.extras')
            extras.mkdir()
            output, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            self.assertIsNotNone(exc)
            self.assertIn('directories would be moved with it', output)
            self.assertTrue(old_path.exists())
            self.assertTrue(extras.is_dir())


class BackfillReviewFollowUp4TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Fourth review pass: non-file current paths, dangling symlinks at
        destinations, in-flight downloads for any path-changing overlay
        (and before a cascade-enabled save), and old-stem leftovers
        beside a target that kept its directory.
    '''

    ACODEC_OVERLAY = '{"*": {"source_acodec": "MP4A"}}'

    def make_busy_source(self):
        source = make_bridge_source()
        source.make_directory()
        busy = Media.objects.create(key='busy1', source=source, metadata=metadata)
        Media.objects.filter(pk=busy.pk).update(skip=False, manual_skip=False)
        return source, busy

    @contextmanager
    def locked(self, media):
        lock = huey_lock_task(f'media:{media.uuid}', queue=Val(TaskQueue.DB))
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def test_a_directory_as_the_current_file_is_refused(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, media, old_path = self.make_downloaded()
            old_path.unlink()
            old_path.mkdir()
            (old_path / 'inner.txt').write_bytes(b'x')
            output, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            self.assertIsNotNone(exc)
            self.assertIn('is not a regular file', output)
            self.assertTrue((old_path / 'inner.txt').exists())
            media.refresh_from_db()
            self.assertEqual(Path(media.media_file.path), old_path)

    def test_a_dangling_symlink_at_the_target_is_occupied(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, media, old_path = self.make_downloaded()
            target = self.target_dir(source) / f'{self.TARGET_NAME}.mkv'
            target.parent.mkdir(parents=True)
            target.symlink_to(source.directory_path / 'missing.mkv')
            output, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            self.assertIsNotNone(exc)
            self.assertIn('is already occupied', output)
            self.assertTrue(target.is_symlink())
            self.assertTrue(old_path.exists())

    def test_a_dangling_symlink_at_a_sidecar_destination_is_occupied(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, media, old_path = self.make_downloaded()
            old_path.with_suffix('.info.json').write_text('{}')
            destination = self.target_dir(source) / f'{self.TARGET_NAME}.info.json'
            destination.parent.mkdir(parents=True)
            destination.symlink_to(source.directory_path / 'missing.json')
            output, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            self.assertIsNotNone(exc)
            self.assertIn('sidecar target(s) already occupied', output)
            self.assertTrue(destination.is_symlink())
            self.assertTrue(old_path.exists())

    def test_in_flight_is_counted_for_an_acodec_only_overlay(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict(
                'os.environ',
                {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': self.ACODEC_OVERLAY},
            ),
        ):
            source, busy = self.make_busy_source()
            with self.locked(busy):
                output, exc = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            self.assertIsNotNone(exc)
            self.assertIn("source_acodec: 'OPUS' -> 'MP4A'", output)
            self.assertIn('in_flight: 1', output)
            source.refresh_from_db()
            self.assertEqual(source.source_acodec, 'MP4A')  # cascade off: saved

    def test_an_in_flight_download_refuses_a_cascade_enabled_save(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=True, RENAME_SOURCES=[]),
            patch.dict(
                'os.environ',
                {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': self.ACODEC_OVERLAY},
            ),
        ):
            source, busy = self.make_busy_source()
            with self.locked(busy):
                dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
                applied, exc = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn(f'IN FLIGHT: {busy}', output)
                self.assertIn('1 media item(s) are downloading right now', output)
                self.assertIn('TUBESYNC_RENAME_ALL_SOURCES=false', output)
                self.assertIn('in_flight: 1', output)
                self.assertIn('errors: 0', output)
            self.assertIn('NOTE: --apply would skip this source', dry)
            self.assertIn('SKIPPED', applied)
            self.assertEqual(summary_of(dry), summary_of(applied))
            source.refresh_from_db()
            self.assertEqual(source.source_acodec, 'OPUS')  # never saved

    def test_an_old_stem_sidecar_beside_a_same_directory_target_is_reported(self):
        overlay = '{"*": {"media_format": "{key}.{ext}"}}'
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source, media, old_path = self.make_downloaded()
            # An earlier run moved and saved the video, then failed before
            # moving its old-stem subtitle; the target kept its directory.
            target = old_path.with_name('vid1.mkv')
            old_path.rename(target)
            media.media_file.name = str(
                target.relative_to(media_file_storage.location)
            )
            media.save(update_fields=('media_file',))
            stray = old_path.with_name(old_path.stem + '.en.srt')
            stray.write_bytes(b'subtitle')
            completed = target.with_name('vid1.en.srt')
            completed.write_bytes(b'subtitle')
            output, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            self.assertIsNotNone(exc)
            self.assertIn('leftover sidecar(s)', output)
            self.assertIn(str(stray), output)
            self.assertNotIn(str(completed), output)
            self.assertTrue(stray.exists())


class BackfillReviewFollowUp5TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Fifth review pass: already-in-place media get the rename's path
        checks, destinations an earlier media claims (or, in a dry-run,
        is projected to claim) are occupied, and a symlinked tvshow.nfo is
        never replaced.
    '''

    def place(self, source):
        '''Runs one apply that renames the media into place.'''
        run_backfill('--source', str(source.uuid), '--apply')

    def test_a_directory_at_an_in_place_target_is_refused(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            self.place(source)
            target = self.target_dir(source) / f'{self.TARGET_NAME}.mkv'
            nfo = self.target_dir(source) / f'{self.TARGET_NAME}.nfo'
            target.unlink()
            target.mkdir()
            nfo.unlink()
            output, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            self.assertIsNotNone(exc)
            self.assertIn('not in place', output)
            self.assertIn('is not a regular file', output)
            self.assertIn('already_in_place: 0', output)
            self.assertFalse(nfo.exists())

    def test_an_in_place_media_reached_through_an_outside_symlink_is_refused(self):
        with temp_download_root(), tempfile.TemporaryDirectory() as outside:
            source, media, old_path = self.make_downloaded()
            self.place(source)
            moved = Path(outside) / 'Season 2017'
            self.target_dir(source).rename(moved)
            self.target_dir(source).symlink_to(moved)
            nfo = moved / f'{self.TARGET_NAME}.nfo'
            nfo.unlink()
            output, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            self.assertIsNotNone(exc)
            self.assertIn('not in place', output)
            self.assertIn('resolves outside', output)
            self.assertFalse(nfo.exists())

    def test_a_sidecar_onto_an_earlier_medias_projected_video_is_refused(self):
        names = {'aaa': 'foo.en.mkv', 'bbb': 'foo.mkv'}
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, first, _ = self.make_downloaded(key='aaa')
            _, second, second_path = self.make_downloaded(key='bbb', source=source)
            # bbb's old .en.mkv sidecar would move to foo.en.mkv, aaa's target.
            sidecar = second_path.with_name(second_path.stem + '.en.mkv')
            sidecar.write_bytes(b'subtitle track')
            with patch.object(
                Media, 'filename', property(lambda media: names[media.key]),
            ):
                dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
                applied, exc = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('sidecar target(s) already occupied', output)
                self.assertIn('renamed: 1', output)
                self.assertIn('errors: 1', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            self.assertEqual(sidecar.read_bytes(), b'subtitle track')
            self.assertEqual(
                (source.directory_path / 'foo.en.mkv').read_bytes(),
                b'fake-mkv-bytes',
            )

    def test_a_video_onto_an_earlier_medias_projected_sidecar_is_refused(self):
        names = {'aaa': 'foo.mkv', 'bbb': 'foo.en.mkv'}
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, first, first_path = self.make_downloaded(key='aaa')
            self.make_downloaded(key='bbb', source=source)
            # aaa's old .en.mkv sidecar moves to foo.en.mkv, bbb's target.
            sidecar = first_path.with_name(first_path.stem + '.en.mkv')
            sidecar.write_bytes(b'subtitle track')
            with patch.object(
                Media, 'filename', property(lambda media: names[media.key]),
            ):
                dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
                applied, exc = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('is already occupied', output)
                self.assertIn('renamed: 1', output)
                self.assertIn('errors: 1', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            self.assertEqual(
                (source.directory_path / 'foo.en.mkv').read_bytes(),
                b'subtitle track',
            )

    def test_a_dangling_tvshow_nfo_symlink_survives_apply(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            tvshow = source.directory_path / 'tvshow.nfo'
            tvshow.symlink_to(source.directory_path / 'missing.nfo')
            dry = run_backfill('--source', str(source.uuid))
            applied = run_backfill('--source', str(source.uuid), '--apply')
            for output in (dry, applied):
                self.assertIn('tvshow_written: 0', output)
            self.assertTrue(tvshow.is_symlink())
            self.assertFalse(tvshow.exists())
