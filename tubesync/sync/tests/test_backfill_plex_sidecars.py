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
import shutil
import signal
import tempfile
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch
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


def run_backfill_refused(*args, **options):
    '''run_backfill() for a run that must exit non-zero; returns its output.'''
    output, exc = run_backfill_capture(*args, **options)
    assert exc is not None, 'expected the backfill to exit non-zero'
    return output


def locked_on_entry(message):
    '''
        Patches the command's huey_lock_task so every lock it takes is
        held by another task (entering it raises TaskLockedException), as
        when a task takes the lock after the in-flight check saw it free.
    '''
    lock = MagicMock()
    lock.is_locked.return_value = False
    lock.__enter__.side_effect = TaskLockedException(message)
    return patch(
        'sync.management.commands.medianest_backfill_plex_sidecars.huey_lock_task',
        return_value=lock,
    )


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

            output, error = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            self.assertIsNotNone(error)
            # Review pass 30: two rows rendering to one target now refuse
            # the whole source up front, before either row moves.
            self.assertIn('is also the target of', output)

            first.refresh_from_db()
            second.refresh_from_db()
            self.assertNotEqual(
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
                locked_on_entry('busy'),
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
            with locked_on_entry('busy'):
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
            # Review pass 30: the shared target now refuses the whole source
            # up front, in the dry-run as in apply.
            self.assertIn('renamed: 0', dry)
            self.assertIn('errors: 1', dry)
            self.assertIn('is also the target of', dry)

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
            self.assertIn('holds symlinks or special files', output)
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
            self.assertIn('holds symlinks or special files', output)
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
            with locked_on_entry('unable to acquire lock media:x'):
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
            self.assertIn('holds symlinks or special files', output)
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
            self.assertIn('holds symlinks or special files', output)
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
            # Review pass 51: a format without the whole {key} is refused
            # up front (the episode fields are not unique per media).
            self.assertIn('does not use the whole {key}', output)
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
            self.assertIn('holds symlinks or special files', output)
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
            self.assertIn('holds symlinks or special files', output)
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
            # Review pass 14's source-level check refuses a row recorded
            # through a symlinked directory before this per-media one.
            self.assertIn('holds symlinks or special files', output)
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
                # Review pass 36: move destinations are claimed across media,
                # so the whole source is refused before either media moves.
                self.assertIn('is also used by aaa', output)
                self.assertIn('renamed: 0', output)
                self.assertIn('errors: 1', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            self.assertEqual(sidecar.read_bytes(), b'subtitle track')
            self.assertFalse((source.directory_path / 'foo.en.mkv').exists())

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
                # Review pass 36: move destinations are claimed across media,
                # so the whole source is refused before either media moves.
                self.assertIn('is a sidecar of aaa', output)
                self.assertIn('renamed: 0', output)
                self.assertIn('errors: 1', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            self.assertEqual(sidecar.read_bytes(), b'subtitle track')
            self.assertFalse((source.directory_path / 'foo.en.mkv').exists())

    def test_a_dangling_tvshow_nfo_symlink_survives_apply(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            tvshow = source.directory_path / 'tvshow.nfo'
            tvshow.symlink_to(source.directory_path / 'missing.nfo')
            dry = run_backfill_refused('--source', str(source.uuid))
            applied = run_backfill_refused('--source', str(source.uuid), '--apply')
            for output in (dry, applied):
                self.assertIn('tvshow_written: 0', output)
            self.assertTrue(tvshow.is_symlink())
            self.assertFalse(tvshow.exists())


class BackfillReviewFollowUp6TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Sixth review pass: a foreign NFO a move would carry onto the target
        NFO name, a symlinked poster.jpg, and a leftover whose old stem
        starts with the new one.
    '''

    FOREIGN = '<episodedetails><title>Mine</title></episodedetails>'

    def assert_refused_in_both_modes(self, source, message):
        dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
        applied, exc = run_backfill_capture(
            '--source', str(source.uuid), '--apply',
        )
        for output, error in ((dry, dry_exc), (applied, exc)):
            self.assertIsNotNone(error)
            self.assertIn(message, output)
            self.assertIn('renamed: 0', output)
        self.assertEqual(summary_of(dry), summary_of(applied))

    def test_a_foreign_nfo_beside_the_old_video_is_not_moved(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            old_nfo = old_path.with_suffix('.nfo')
            old_nfo.write_text(self.FOREIGN, encoding='utf-8')
            self.assert_refused_in_both_modes(
                source, 'would be moved to its new name and then overwritten',
            )
            self.assertEqual(old_nfo.read_text(encoding='utf-8'), self.FOREIGN)
            self.assertTrue(old_path.exists())

    def test_a_foreign_key_matched_nfo_is_not_moved(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            orphan = source.directory_path / 'leftovers' / 'old name [vid1].nfo'
            orphan.parent.mkdir()
            orphan.write_text(self.FOREIGN, encoding='utf-8')
            self.assert_refused_in_both_modes(
                source, 'would be moved to its new name and then overwritten',
            )
            self.assertEqual(orphan.read_text(encoding='utf-8'), self.FOREIGN)

    def test_an_nfo_in_an_unreadable_encoding_is_not_moved(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            old_nfo = old_path.with_suffix('.nfo')
            raw = (
                b'<?xml version="1.0" encoding="ANSI"?>'
                b'<episodedetails><id>vid1</id></episodedetails>'
            )
            old_nfo.write_bytes(raw)
            self.assert_refused_in_both_modes(
                source, 'would be moved to its new name and then overwritten',
            )
            self.assertEqual(old_nfo.read_bytes(), raw)

    def test_the_medias_own_old_nfo_still_moves(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            old_nfo = old_path.with_suffix('.nfo')
            old_nfo.write_text(
                '<episodedetails><uniqueid type="youtube">vid1</uniqueid>'
                '</episodedetails>',
                encoding='utf-8',
            )
            output = run_backfill('--source', str(source.uuid), '--apply')
            self.assertIn('renamed: 1', output)
            self.assertFalse(old_nfo.exists())
            nfo = self.target_dir(source) / f'{self.TARGET_NAME}.nfo'
            self.assertEqual(
                ElementTree.parse(nfo).getroot().findtext('title'),
                'no fancy stuff title',
            )

    def test_a_dangling_poster_symlink_queues_no_image_download(self):
        with temp_download_root(), tempfile.TemporaryDirectory() as outside:
            source = make_bridge_source(copy_channel_images=True)
            source.make_directory()
            self.make_downloaded(source=source)
            poster = source.directory_path / 'poster.jpg'
            poster.symlink_to(Path(outside) / 'poster.jpg')
            with patch(f'{self.COMMAND}.TaskHistory') as mock_th:
                dry = run_backfill_refused('--source', str(source.uuid))
                applied = run_backfill_refused('--source', str(source.uuid), '--apply')
            mock_th.schedule.assert_not_called()
            for output in (dry, applied):
                self.assertIn('images_enqueued: 0', output)
                self.assertIn('holds symlinks or special files', output)
            self.assertTrue(poster.is_symlink())
            self.assertFalse((Path(outside) / 'poster.jpg').exists())

    def test_a_leftover_starting_with_the_new_stem_is_found(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            run_backfill('--source', str(source.uuid), '--apply')
            target_dir = self.target_dir(source)
            own = target_dir / f'{self.TARGET_NAME}.en.srt'
            own.write_bytes(b'subtitle')
            leftover = target_dir / f'{self.TARGET_NAME}-old.en.srt'
            leftover.write_bytes(b'old subtitle')
            output, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            self.assertIsNotNone(exc)
            self.assertIn('leftover sidecar(s)', output)
            self.assertIn(str(leftover), output)
            self.assertNotIn(str(own), output)
            self.assertIn('already_in_place: 0', output)


class BackfillReviewFollowUp7TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Seventh review pass: adoption never takes another media's file, and
        stem-pass moves never take another media's sidecar.
    '''

    def run_both(self, source, names):
        with patch.object(
            Media, 'filename', property(lambda media: names[media.key]),
        ):
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
        self.assertIsNotNone(dry_exc)
        self.assertIsNotNone(exc)
        self.assertEqual(summary_of(dry), summary_of(applied))
        return dry, applied

    def test_a_sidecar_moved_onto_a_missing_medias_target_is_not_adopted(self):
        names = {'aaa': 'foo.mkv', 'bbb': 'foo.en.mkv'}
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, first, first_path = self.make_downloaded(key='aaa')
            _, second, second_path = self.make_downloaded(key='bbb', source=source)
            # aaa's old .en.mkv sidecar moves to foo.en.mkv, bbb's target,
            # and bbb's own video is gone (a half-finished earlier run).
            sidecar = first_path.with_name(first_path.stem + '.en.mkv')
            sidecar.write_bytes(b'subtitle track')
            second_path.unlink()
            dry, applied = self.run_both(source, names)
            for output in (dry, applied):
                # Review pass 36: move destinations are claimed across media,
                # so the whole source is refused before either media moves.
                self.assertIn('is a sidecar of aaa', output)
                self.assertIn('adopted: 0', output)
                self.assertIn('renamed: 0', output)
            second.refresh_from_db()
            self.assertEqual(Path(second.media_file.path), second_path)
            self.assertEqual(sidecar.read_bytes(), b'subtitle track')
            self.assertFalse((source.directory_path / 'foo.en.mkv').exists())

    def test_another_medias_existing_sidecar_is_not_adopted(self):
        names = {'aaa': 'foo.mkv', 'bbb': 'foo.en.mkv'}
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, first, first_path = self.make_downloaded(key='aaa')
            _, second, second_path = self.make_downloaded(key='bbb', source=source)
            # aaa already sits at foo.mkv with its foo.en.mkv subtitle; bbb's
            # video is gone and its target is that subtitle.
            placed = source.directory_path / 'foo.mkv'
            first_path.rename(placed)
            first.media_file.name = str(placed.relative_to(media_file_storage.location))
            first.save()
            subtitle = source.directory_path / 'foo.en.mkv'
            subtitle.write_bytes(b'subtitle track')
            second_path.unlink()
            dry, applied = self.run_both(source, names)
            for output in (dry, applied):
                self.assertIn('belongs to another media', output)
                self.assertIn('adopted: 0', output)
            second.refresh_from_db()
            self.assertEqual(Path(second.media_file.path), second_path)
            self.assertEqual(subtitle.read_bytes(), b'subtitle track')

    def test_a_stem_match_that_is_a_missing_medias_sidecar_is_not_moved(self):
        names = {'aaa': 'foo.mkv', 'bbb': 'bar.mkv'}
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, first, first_path = self.make_downloaded(key='aaa')
            # bbb's recorded video is "<aaa's stem>.bar.mkv" and is gone,
            # but its subtitle remains; aaa's stem glob matches it.
            second = Media.objects.create(key='bbb', source=source, metadata=metadata)
            second_video = first_path.with_name(first_path.stem + '.bar.mkv')
            second.media_file.name = str(
                second_video.relative_to(media_file_storage.location)
            )
            second.downloaded = True
            second.save()
            subtitle = first_path.with_name(first_path.stem + '.bar.srt')
            subtitle.write_bytes(b'bbb subtitle')
            dry, applied = self.run_both(source, names)
            for output in (dry, applied):
                self.assertIn('other media files would be moved with it', output)
                self.assertIn(str(subtitle), output)
                self.assertIn('renamed: 0', output)
            self.assertEqual(subtitle.read_bytes(), b'bbb subtitle')
            self.assertTrue(first_path.exists())


class BackfillReviewFollowUp8TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Eighth review pass: every file the channel image download writes
        is checked before it is queued, and a download marked skipped
        while it runs still counts as in flight.
    '''

    ACODEC_OVERLAY = BackfillReviewFollowUp4TestCase.ACODEC_OVERLAY
    make_busy_source = BackfillReviewFollowUp4TestCase.make_busy_source
    locked = BackfillReviewFollowUp4TestCase.locked

    def test_a_download_marked_skipped_while_running_is_in_flight(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=True, RENAME_SOURCES=[]),
            patch.dict(
                'os.environ',
                {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': self.ACODEC_OVERLAY},
            ),
        ):
            source, busy = self.make_busy_source()
            Media.objects.filter(pk=busy.pk).update(skip=True, manual_skip=True)
            with self.locked(busy):
                dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
                applied, exc = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn(f'IN FLIGHT: {busy}', output)
                self.assertIn('in_flight: 1', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            source.refresh_from_db()
            self.assertEqual(source.source_acodec, 'OPUS')  # never saved

    def test_a_symlinked_image_destination_queues_no_image_download(self):
        with temp_download_root(), tempfile.TemporaryDirectory() as outside:
            source = make_bridge_source(copy_channel_images=True)
            source.make_directory()
            self.make_downloaded(source=source)
            # poster.jpg stays missing, so only the link can stop the job.
            for name in ('thumbnail.jpg', 'banner.jpg', 'background.jpg',
                         'season-poster.jpg'):
                with self.subTest(name=name):
                    link = source.directory_path / name
                    link.symlink_to(Path(outside) / name)
                    with patch(f'{self.COMMAND}.TaskHistory') as mock_th:
                        dry = run_backfill_refused('--source', str(source.uuid))
                        applied = run_backfill_refused(
                            '--source', str(source.uuid), '--apply',
                        )
                    mock_th.schedule.assert_not_called()
                    for output in (dry, applied):
                        self.assertIn('images_enqueued: 0', output)
                        self.assertIn('holds symlinks or special files', output)
                    self.assertFalse((Path(outside) / name).exists())
                    link.unlink()

    def test_a_source_directory_outside_the_root_queues_no_image_download(self):
        with temp_download_root(), tempfile.TemporaryDirectory() as outside:
            source = make_bridge_source(copy_channel_images=True)
            directory = source.directory_path
            directory.parent.mkdir(parents=True, exist_ok=True)
            directory.symlink_to(outside, target_is_directory=True)
            with patch(f'{self.COMMAND}.TaskHistory') as mock_th:
                dry, _ = run_backfill_capture('--source', str(source.uuid))
                applied, _ = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            mock_th.schedule.assert_not_called()
            for output in (dry, applied):
                self.assertIn('images_enqueued: 0', output)
                self.assertIn('holds symlinks or special files', output)
            self.assertFalse((Path(outside) / 'poster.jpg').exists())


class BackfillReviewFollowUp9TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Ninth review pass: a source directory outside DOWNLOAD_ROOT writes
        nothing in either mode, and a missing source directory that no save
        recreates queues no image download.
    '''

    def test_a_source_directory_outside_the_root_writes_nothing(self):
        with temp_download_root(), tempfile.TemporaryDirectory() as outside:
            source = make_bridge_source(copy_channel_images=True)
            directory = source.directory_path
            directory.parent.mkdir(parents=True, exist_ok=True)
            directory.symlink_to(outside, target_is_directory=True)
            with patch(f'{self.COMMAND}.TaskHistory') as mock_th:
                dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
                applied, exc = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            mock_th.schedule.assert_not_called()
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('holds symlinks or special files', output)
                self.assertIn('tvshow_written: 0', output)
                self.assertIn('images_enqueued: 0', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_a_missing_directory_nothing_recreates_queues_no_download(self):
        with temp_download_root():
            source = make_bridge_source(copy_channel_images=True)
            source.make_directory()
            with patch(f'{self.COMMAND}.TaskHistory'):
                run_backfill('--source', str(source.uuid), '--apply')
            # The profile is applied, so the next run saves nothing and
            # nothing recreates the directory.
            shutil.rmtree(source.directory_path)
            with patch(f'{self.COMMAND}.TaskHistory') as mock_th:
                dry = run_backfill('--source', str(source.uuid))
                applied = run_backfill('--source', str(source.uuid), '--apply')
            mock_th.schedule.assert_not_called()
            for output in (dry, applied):
                self.assertIn('images_enqueued: 0', output)
                self.assertIn('does not exist', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            self.assertFalse(source.directory_path.exists())

    def test_a_missing_directory_a_save_recreates_still_queues(self):
        with temp_download_root():
            source = make_bridge_source(copy_channel_images=True)
            with patch(f'{self.COMMAND}.TaskHistory') as mock_th:
                dry = run_backfill('--source', str(source.uuid))
                applied = run_backfill('--source', str(source.uuid), '--apply')
            mock_th.schedule.assert_called_once()
            for output in (dry, applied):
                self.assertIn('images_enqueued: 1', output)
                self.assertNotIn('does not exist', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            self.assertTrue(source.directory_path.is_dir())


class BackfillReviewFollowUp10TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Tenth review pass: an overlay that would turn copy_channel_images
        on is refused, in both modes, BEFORE it is ever saved, when a file
        the resulting image download would write is a symlink or the
        source directory resolves outside DOWNLOAD_ROOT. Saving that
        overlay fires source_pre_save, which queues download_source_images
        unconditionally for the same save -- ahead of this command's own
        images_already_queued handling in _process_tvshow_and_images() --
        so the gate must run, and refuse the source, before any save.
    '''

    def test_a_symlinked_image_destination_refuses_the_overlay(self):
        with temp_download_root(), tempfile.TemporaryDirectory() as outside:
            source, media, old_path = self.make_downloaded()
            link = source.directory_path / 'banner.jpg'
            link.symlink_to(Path(outside) / 'banner.jpg')
            with (
                patch(f'{self.COMMAND}.TaskHistory') as mock_th,
                patch('sync.signals.download_source_images') as mock_signal,
            ):
                dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
                applied, exc = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            mock_th.schedule.assert_not_called()
            mock_signal.assert_not_called()
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('errors: 1', output)
                self.assertIn('holds symlinks or special files', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            source.refresh_from_db()
            self.assertFalse(source.copy_channel_images)
            self.assertFalse((Path(outside) / 'banner.jpg').exists())

    def test_a_source_directory_outside_the_root_refuses_the_overlay(self):
        with temp_download_root(), tempfile.TemporaryDirectory() as outside:
            source = make_bridge_source()
            directory = source.directory_path
            directory.parent.mkdir(parents=True, exist_ok=True)
            directory.symlink_to(outside, target_is_directory=True)
            with (
                patch(f'{self.COMMAND}.TaskHistory') as mock_th,
                patch('sync.signals.download_source_images') as mock_signal,
            ):
                dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
                applied, exc = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            mock_th.schedule.assert_not_called()
            mock_signal.assert_not_called()
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('errors: 1', output)
                self.assertIn('holds symlinks or special files', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            source.refresh_from_db()
            self.assertFalse(source.copy_channel_images)
            self.assertEqual(list(Path(outside).iterdir()), [])


class BackfillReviewFollowUp11TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Eleventh review pass: a source directory path taken by something
        that is not a directory is refused the same way in both modes.
    '''

    def assert_refused(self, occupy):
        with temp_download_root():
            source = make_bridge_source()
            directory = source.directory_path
            directory.parent.mkdir(parents=True, exist_ok=True)
            occupy(directory)
            with patch(f'{self.COMMAND}.TaskHistory') as mock_th:
                dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
                applied, exc = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            mock_th.schedule.assert_not_called()
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('exists but is not a directory', output)
                self.assertIn('errors: 1', output)
                self.assertIn('tvshow_written: 0', output)
                self.assertIn('images_enqueued: 0', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            source.refresh_from_db()
            # The profile turns copy_channel_images on; nothing was saved.
            self.assertFalse(source.copy_channel_images)
            self.assertFalse(directory.is_dir())

    def test_a_regular_file_at_the_source_path_is_refused(self):
        self.assert_refused(lambda path: path.write_bytes(b'not a directory'))

    def test_a_dangling_symlink_at_the_source_path_is_refused(self):
        self.assert_refused(
            lambda path: path.symlink_to(path.parent / 'missing'),
        )


class BackfillReviewFollowUp12TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Twelfth review pass: a target directory reached through a symlink
        inside DOWNLOAD_ROOT is refused before anything moves, and image
        destinations that are not regular files block the image download.
    '''

    def test_a_symlinked_target_directory_inside_the_root_is_refused(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, media, old_path = self.make_downloaded()
            real = source.directory_path / 'real-season'
            real.mkdir()
            self.target_dir(source).symlink_to(real, target_is_directory=True)
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('holds symlinks or special files', output)
                self.assertIn('renamed: 0', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            self.assertTrue(old_path.exists())
            self.assertEqual(list(real.iterdir()), [])
            media.refresh_from_db()
            self.assertEqual(Path(media.media_file.path), old_path)

    def test_a_non_file_image_destination_queues_no_image_download(self):
        with temp_download_root():
            source = make_bridge_source(copy_channel_images=True)
            source.make_directory()
            self.make_downloaded(source=source)
            banner = source.directory_path / 'banner.jpg'
            thumbnail = source.directory_path / 'thumbnail.jpg'
            for name, make, undo in (
                ('banner.jpg', banner.mkdir, banner.rmdir),
                ('thumbnail.jpg', lambda: os.mkfifo(thumbnail), thumbnail.unlink),
            ):
                with self.subTest(name=name):
                    make()
                    with patch(f'{self.COMMAND}.TaskHistory') as mock_th:
                        # A refused source exits non-zero; only the output
                        # matters here.
                        dry = run_backfill_capture('--source', str(source.uuid))[0]
                        applied = run_backfill_capture(
                            '--source', str(source.uuid), '--apply',
                        )[0]
                    mock_th.schedule.assert_not_called()
                    for output in (dry, applied):
                        self.assertIn('images_enqueued: 0', output)
                        # A directory named like an image is a real
                        # directory and meets the per-path check; a FIFO is
                        # a special file, refused by the source-tree
                        # precondition (review pass 16) first.
                        self.assertIn(
                            f'{name} exists but is not a regular file'
                            if name == 'banner.jpg'
                            else 'holds symlinks or special files',
                            output,
                        )
                    undo()

    def test_a_non_file_image_destination_refuses_the_overlay(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            (source.directory_path / 'banner.jpg').mkdir()
            with (
                patch(f'{self.COMMAND}.TaskHistory') as mock_th,
                patch('sync.signals.download_source_images') as mock_signal,
            ):
                dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
                applied, exc = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            mock_th.schedule.assert_not_called()
            mock_signal.assert_not_called()
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('exists but is not a regular file', output)
                self.assertIn('errors: 1', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            source.refresh_from_db()
            self.assertFalse(source.copy_channel_images)


class _ReadBlocked(BaseException):
    '''Raised by a test alarm when a read blocks on a FIFO.'''


class BackfillReviewFollowUp13TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Thirteenth review pass: a current path under a symlinked directory
        inside DOWNLOAD_ROOT is refused like a symlinked target, and NFO
        paths that are not regular files are foreign and never read.
    '''

    FOREIGN = 'not this media'

    def setUp(self):
        super().setUp()
        # Reading a FIFO blocks; fail the test instead of hanging the run.
        signal.signal(signal.SIGALRM, self._timed_out)
        signal.alarm(20)

    def tearDown(self):
        signal.alarm(0)

    @staticmethod
    def _timed_out(signum, frame):
        # A BaseException, so the command's broad `except Exception`
        # handlers cannot swallow it and block on the next read.
        raise _ReadBlocked('an NFO read blocked')

    def run_both(self, source):
        dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
        applied, exc = run_backfill_capture(
            '--source', str(source.uuid), '--apply',
        )
        self.assertEqual(summary_of(dry), summary_of(applied))
        return (dry, dry_exc), (applied, exc)

    def test_a_current_path_under_a_symlinked_directory_is_refused(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, media, old_path = self.make_downloaded()
            real = source.directory_path / 'real-old'
            real.mkdir()
            moved = real / old_path.name
            old_path.rename(moved)
            alias = source.directory_path / 'old-alias'
            alias.symlink_to(real, target_is_directory=True)
            aliased = alias / old_path.name
            media.media_file.name = str(
                aliased.relative_to(media_file_storage.location)
            )
            media.save()
            for output, error in self.run_both(source):
                self.assertIsNotNone(error)
                # The source-level check (review pass 14) now refuses it
                # before the per-media one is reached.
                self.assertIn('holds symlinks or special files', output)
                self.assertIn(str(alias), output)
                self.assertIn('renamed: 0', output)
            self.assertTrue(moved.exists())
            media.refresh_from_db()
            self.assertEqual(Path(media.media_file.path), aliased)

    def test_a_fifo_at_the_target_episode_nfo_is_foreign(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            run_backfill('--source', str(source.uuid), '--apply')
            nfo = self.target_dir(source) / f'{self.TARGET_NAME}.nfo'
            nfo.unlink()
            os.mkfifo(nfo)
            for output, error in self.run_both(source):
                self.assertIsNotNone(error)
                self.assertIn('holds symlinks or special files', output)
            self.assertFalse(nfo.is_file())

    def test_a_fifo_nfo_beside_the_old_video_is_not_moved(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            old_nfo = old_path.with_suffix('.nfo')
            os.mkfifo(old_nfo)
            for output, error in self.run_both(source):
                self.assertIsNotNone(error)
                # The source-tree precondition (review pass 16) refuses a
                # FIFO before the per-media check below it is reached.
                self.assertIn('holds symlinks or special files', output)
                self.assertIn('renamed: 0', output)
            self.assertTrue(old_path.exists())
            self.assertFalse(old_nfo.is_file())

    def test_a_fifo_tvshow_nfo_is_never_read(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            tvshow = source.directory_path / 'tvshow.nfo'
            os.mkfifo(tvshow)
            (dry, _), (applied, _) = self.run_both(source)
            for output in (dry, applied):
                self.assertIn('tvshow_written: 0', output)
            self.assertFalse(tvshow.is_file())


@contextmanager
def symlinked_download_root():
    '''Like temp_download_root(), but DOWNLOAD_ROOT is a symlink.'''
    with tempfile.TemporaryDirectory() as tmp_dir:
        real = Path(tmp_dir) / 'real'
        real.mkdir()
        link = Path(tmp_dir) / 'link'
        link.symlink_to(real, target_is_directory=True)
        with (
            override_settings(DOWNLOAD_ROOT=str(link)),
            patch.object(media_file_storage, 'location', str(link)),
        ):
            yield link


class BackfillReviewFollowUp14TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Fourteenth review pass: a storage location reached through a
        symlink refuses the whole run, and a source with any row recorded
        through a symlinked directory is refused before anything moves.
    '''

    def test_a_symlinked_storage_location_refuses_the_run(self):
        with symlinked_download_root():
            source, media, old_path = self.make_downloaded()
            for args in ((), ('--apply',)):
                with self.subTest(args=args):
                    output, error = run_backfill_capture(
                        '--source', str(source.uuid), *args,
                    )
                    self.assertIsNotNone(error)
                    self.assertIn('goes through a symlink', str(error))
                    self.assertIn('nothing was changed', str(error))
            self.assertTrue(old_path.exists())
            media.refresh_from_db()
            self.assertEqual(Path(media.media_file.path), old_path)

    def test_another_row_recorded_through_an_alias_refuses_the_source(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, first, first_path = self.make_downloaded(key='aaa')
            real = source.directory_path / 'real-dir'
            real.mkdir()
            alias = source.directory_path / 'alias'
            alias.symlink_to(real, target_is_directory=True)
            second = Media.objects.create(key='bbb', source=source, metadata=metadata)
            (real / 'other.mkv').write_bytes(b'other video')
            second.media_file.name = str(
                (alias / 'other.mkv').relative_to(media_file_storage.location)
            )
            second.downloaded = True
            second.save()
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('holds symlinks or special files', output)
                self.assertIn(str(alias), output)
                self.assertIn('renamed: 0', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            self.assertTrue(first_path.exists())
            self.assertEqual((real / 'other.mkv').read_bytes(), b'other video')
            source.refresh_from_db()
            self.assertFalse(source.copy_channel_images)  # never saved


class BackfillReviewFollowUp15TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Fifteenth review pass: a source whose directory contains another
        source's directory is refused when the {key} sweep could reach it.
    '''

    def test_a_nested_source_directory_refuses_the_source(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, media, old_path = self.make_downloaded()
            nested_source = make_bridge_source(
                key='UCnestedabcdefghijklmnop',
                name='acq-src-nested',
                directory=f'{source.directory}/nested',
            )
            nested_source.make_directory()
            # The same video, downloaded by the nested source too.
            nested_row = Media.objects.create(
                key='vid1', source=nested_source, metadata=metadata,
            )
            nested_file = nested_source.directory_path / 'copy [vid1].mkv'
            nested_file.write_bytes(b'nested copy')
            nested_row.media_file.name = str(
                nested_file.relative_to(media_file_storage.location)
            )
            nested_row.downloaded = True
            nested_row.save()
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn("other sources' directories", output)
                self.assertIn('renamed: 0', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            self.assertEqual(nested_file.read_bytes(), b'nested copy')
            self.assertTrue(old_path.exists())
            source.refresh_from_db()
            self.assertFalse(source.copy_channel_images)  # never saved

    def test_a_sibling_source_directory_is_not_nested(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, media, old_path = self.make_downloaded()
            make_bridge_source(
                key='UCsiblingabcdefghijklmno',
                name='acq-src-sibling',
                directory=f'{source.directory}-sibling',
            )
            output = run_backfill('--source', str(source.uuid), '--apply')
            self.assertIn('renamed: 1', output)


class BackfillReviewFollowUp16TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Sixteenth review pass: the source tree must hold only regular files
        and real directories (one precondition for every symlink and
        special-file case), and a row recorded through an alias outside the
        source tree is still refused by the source-level alias check.
    '''

    def assert_refused(self, source, message):
        dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
        applied, exc = run_backfill_capture(
            '--source', str(source.uuid), '--apply',
        )
        for output, error in ((dry, dry_exc), (applied, exc)):
            self.assertIsNotNone(error)
            self.assertIn(message, output)
            self.assertIn('renamed: 0', output)
            self.assertIn('adopted: 0', output)
        self.assertEqual(summary_of(dry), summary_of(applied))

    def test_a_dangling_symlink_sidecar_refuses_the_source(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            sidecar = old_path.with_suffix('.en.srt')
            sidecar.symlink_to(source.directory_path / 'missing.srt')
            self.assert_refused(source, 'holds symlinks or special files')
            self.assertTrue(old_path.exists())
            self.assertTrue(sidecar.is_symlink())

    def test_an_adoption_through_a_symlinked_directory_is_refused(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, media, old_path = self.make_downloaded()
            real = source.directory_path / 'real-season'
            real.mkdir()
            self.target_dir(source).symlink_to(real, target_is_directory=True)
            # A half-finished move: the video already sits at the target
            # (through the link) and the recorded current file is gone.
            old_path.rename(real / f'{self.TARGET_NAME}.mkv')
            self.assert_refused(source, 'holds symlinks or special files')
            media.refresh_from_db()
            self.assertEqual(Path(media.media_file.path), old_path)

    def test_a_row_aliased_outside_the_source_tree_refuses_the_source(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, first, first_path = self.make_downloaded(key='aaa')
            elsewhere = source.directory_path.parent / 'elsewhere'
            elsewhere.mkdir()
            alias = source.directory_path.parent / 'elsewhere-alias'
            alias.symlink_to(elsewhere, target_is_directory=True)
            (elsewhere / 'other.mkv').write_bytes(b'other video')
            second = Media.objects.create(key='bbb', source=source, metadata=metadata)
            second.media_file.name = str(
                (alias / 'other.mkv').relative_to(media_file_storage.location)
            )
            second.downloaded = True
            second.save()
            self.assert_refused(source, 'recorded through symlinked directories')
            self.assertTrue(first_path.exists())
            self.assertEqual((elsewhere / 'other.mkv').read_bytes(), b'other video')


class BackfillReviewFollowUp17TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Seventeenth review pass: a source directory path through a symlink
        is refused before the save can create anything, sources resolving
        to the same directory overlap, and a projected .jpg counts as an
        existing thumbnail in a dry-run as it does in apply.
    '''

    def test_a_source_path_through_a_symlinked_ancestor_is_refused(self):
        with temp_download_root(), tempfile.TemporaryDirectory() as outside:
            source = make_bridge_source(directory='via-link/acq-src-x')
            link = source.directory_path.parent
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(outside, target_is_directory=True)
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('goes through a symlink', output)
                self.assertIn('errors: 1', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            self.assertEqual(list(Path(outside).iterdir()), [])
            source.refresh_from_db()
            self.assertFalse(source.copy_channel_images)  # never saved

    def test_a_source_aliasing_this_directory_overlaps_it(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, media, old_path = self.make_downloaded()
            alias_source = make_bridge_source(
                key='UCaliasabcdefghijklmnopq',
                name='acq-src-alias',
                directory='acq-src-alias',
            )
            alias_source.directory_path.symlink_to(
                source.directory_path, target_is_directory=True,
            )
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn("other sources' directories", output)
                self.assertIn('renamed: 0', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            self.assertTrue(old_path.exists())

    def test_a_projected_jpg_counts_as_an_existing_thumbnail(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            old_path.with_suffix('.jpg').write_bytes(b'old thumbnail')
            with (
                patch.object(
                    Media, 'thumb_file_exists',
                    new_callable=PropertyMock, return_value=True,
                ),
                patch.object(Media, 'copy_thumbnail') as mock_copy,
            ):
                dry = run_backfill('--source', str(source.uuid))
                applied = run_backfill('--source', str(source.uuid), '--apply')
            mock_copy.assert_not_called()
            for output in (dry, applied):
                self.assertIn('thumbs_copied: 0', output)
                self.assertIn('renamed: 1', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            target_jpg = self.target_dir(source) / f'{self.TARGET_NAME}.jpg'
            self.assertEqual(target_jpg.read_bytes(), b'old thumbnail')


class BackfillReviewFollowUp18TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Eighteenth review pass: another source resolving to this directory
        overlaps it for every media_format, not only a {key} one (the
        old-stem glob alone could take its files for sidecars).
    '''

    def test_an_aliased_source_overlaps_without_key_in_the_format(self):
        overlay = '{"*": {"media_format": "{yyyy_mm_dd}_{title}.{ext}"}}'
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source, media, old_path = self.make_downloaded()
            alias_source = make_bridge_source(
                key='UCaliasabcdefghijklmnopq',
                name='acq-src-alias',
                directory='acq-src-alias',
            )
            alias_source.directory_path.symlink_to(
                source.directory_path, target_is_directory=True,
            )
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn("other sources' directories", output)
                self.assertIn('renamed: 0', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            self.assertTrue(old_path.exists())


class BackfillReviewFollowUp19TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Nineteenth review pass: a nested source directory overlaps this
        one for every media_format, not only a {key} one (the old-stem
        glob beside a video already in the nested directory could take
        its files for sidecars).
    '''

    def test_a_nested_source_refuses_the_source_without_key_in_the_format(self):
        overlay = '{"*": {"media_format": "{yyyy_mm_dd}_{title}.{ext}"}}'
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source, media, old_path = self.make_downloaded()
            nested_source = make_bridge_source(
                key='UCnestedabcdefghijklmnop',
                name='acq-src-nested',
                directory=f'{source.directory}/nested',
            )
            nested_source.make_directory()
            # The nested source's own downloaded row, recorded in its
            # nested directory.
            nested_row = Media.objects.create(
                key='vid1', source=nested_source, metadata=metadata,
            )
            nested_file = nested_source.directory_path / 'copy [vid1].mkv'
            nested_file.write_bytes(b'nested copy')
            nested_row.media_file.name = str(
                nested_file.relative_to(media_file_storage.location)
            )
            nested_row.downloaded = True
            nested_row.save()
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn("other sources' directories", output)
                self.assertIn('renamed: 0', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            self.assertTrue(old_path.exists())
            self.assertEqual(nested_file.read_bytes(), b'nested copy')


class BackfillReviewFollowUp20TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Twentieth review pass: overlap with another source is refused in
        both directions, and a media_format that renders an episode NFO at
        tvshow.nfo is refused before anything is written.
    '''

    def assert_refused(self, source, message):
        dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
        applied, exc = run_backfill_capture(
            '--source', str(source.uuid), '--apply',
        )
        for output, error in ((dry, dry_exc), (applied, exc)):
            self.assertIsNotNone(error)
            self.assertIn(message, output)
            self.assertIn('renamed: 0', output)
            self.assertIn('tvshow_written: 0', output)
        self.assertEqual(summary_of(dry), summary_of(applied))

    def test_a_source_nested_inside_another_is_refused(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            parent = make_bridge_source(
                key='UCparentabcdefghijklmnop',
                name='acq-src-parent',
                directory='acq-src-parent',
            )
            parent.make_directory()
            child = make_bridge_source(directory='acq-src-parent/child')
            child.make_directory()
            child, media, old_path = self.make_downloaded(source=child)[0:3]
            # The parent's video beside the child's, sharing its old stem.
            parent_file = old_path.with_name(old_path.stem + '.extra.mkv')
            parent_file.write_bytes(b'parent video')
            self.assert_refused(child, "other sources' directories")
            self.assertTrue(old_path.exists())
            self.assertEqual(parent_file.read_bytes(), b'parent video')

    def test_an_episode_nfo_at_tvshow_nfo_is_refused(self):
        overlay = '{"*": {"media_format": "tvshow.{ext}", "write_nfo": true}}'
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source, media, old_path = self.make_downloaded()
            self.assert_refused(source, "its episode NFO would be the show's")
            self.assertTrue(old_path.exists())
            self.assertFalse((source.directory_path / 'tvshow.nfo').exists())
            source.refresh_from_db()
            self.assertFalse(source.write_nfo)  # never saved


class BackfillReviewFollowUp21TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Twenty-first review pass: an episode NFO path equal to the video's
        own target is refused, and a cached thumbnail that is not a regular
        file is never copied.
    '''

    def setUp(self):
        super().setUp()
        # Reading a FIFO blocks; fail the test instead of hanging the run.
        signal.signal(signal.SIGALRM, self._timed_out)
        signal.alarm(30)

    def tearDown(self):
        signal.alarm(0)

    @staticmethod
    def _timed_out(signum, frame):
        raise _ReadBlocked('a thumbnail read blocked')

    def test_an_episode_nfo_at_the_video_target_is_refused(self):
        overlay = '{"*": {"media_format": "{key}.nfo", "write_nfo": true}}'
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source, media, old_path = self.make_downloaded()
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('its episode NFO would be the video file itself', output)
                self.assertIn('renamed: 0', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            self.assertEqual(old_path.read_bytes(), b'fake-mkv-bytes')

    def test_a_non_regular_cached_thumbnail_is_not_copied(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            for kind, make, undo in (
                ('directory', lambda path: path.mkdir(), lambda path: path.rmdir()),
                ('fifo', os.mkfifo, lambda path: path.unlink()),
            ):
                with self.subTest(kind=kind):
                    name = f'thumbs/backfill-test-{kind}-{os.urandom(4).hex()}.jpg'
                    cached = Path(media.thumb.storage.path(name))
                    cached.parent.mkdir(parents=True, exist_ok=True)
                    make(cached)
                    try:
                        # Dimensions recorded, as for a real download:
                        # otherwise Django's ImageField reads the file to
                        # measure it whenever the row loads.
                        Media.objects.filter(pk=media.pk).update(
                            thumb=name, thumb_width=10, thumb_height=10,
                        )
                        with patch.object(Media, 'copy_thumbnail') as mock_copy:
                            dry, dry_exc = run_backfill_capture(
                                '--source', str(source.uuid),
                            )
                            applied, exc = run_backfill_capture(
                                '--source', str(source.uuid), '--apply',
                            )
                        self.assertIsNone(dry_exc, dry)
                        self.assertIsNone(exc, applied)
                        mock_copy.assert_not_called()
                        for output in (dry, applied):
                            self.assertIn('thumbs_copied: 0', output)
                            self.assertIn('not a regular file', output)
                    finally:
                        undo(cached)


class BackfillReviewFollowUp22TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Twenty-second review pass: with channel images on, a video recorded
        or renamed at a channel-image file name refuses the source before
        the save that could queue the image download.
    '''

    def test_a_video_renamed_to_an_image_name_refuses_the_source(self):
        overlay = (
            '{"*": {"media_format": "poster.jpg", '
            '"copy_channel_images": true}}'
        )
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source, media, old_path = self.make_downloaded()
            with (
                patch(f'{self.COMMAND}.TaskHistory') as mock_th,
                patch('sync.signals.download_source_images') as mock_signal,
            ):
                dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
                applied, exc = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            mock_th.schedule.assert_not_called()
            mock_signal.assert_not_called()
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('the channel image download would overwrite', output)
                self.assertIn('renamed: 0', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            self.assertEqual(old_path.read_bytes(), b'fake-mkv-bytes')
            source.refresh_from_db()
            self.assertFalse(source.copy_channel_images)  # never saved


class BackfillReviewFollowUp23TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Twenty-third review pass: generated sidecars are reserved across
        media, an episode thumbnail at a channel-image name counts, and a
        download finishing during the run meets the same checks.
    '''

    def assert_refused(self, source, names, message):
        with patch.object(
            Media, 'filename', property(lambda media: names[media.key]),
        ):
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
        for output, error in ((dry, dry_exc), (applied, exc)):
            self.assertIsNotNone(error)
            self.assertIn(message, output)
            self.assertIn('renamed: 0', output)
        self.assertEqual(summary_of(dry), summary_of(applied))

    def test_two_media_sharing_generated_sidecars_refuse_the_source(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, first, first_path = self.make_downloaded(key='aaa')
            _, second, second_path = self.make_downloaded(key='bbb', source=source)
            self.assert_refused(
                source, {'aaa': 'shared.mp4', 'bbb': 'shared.webm'},
                'is also used by',
            )
            self.assertTrue(first_path.exists())
            self.assertTrue(second_path.exists())
            self.assertFalse((source.directory_path / 'shared.nfo').exists())

    def test_an_episode_thumbnail_at_a_channel_image_name_is_refused(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, media, old_path = self.make_downloaded()
            with (
                patch(f'{self.COMMAND}.TaskHistory') as mock_th,
                patch('sync.signals.download_source_images') as mock_signal,
            ):
                self.assert_refused(
                    source, {'vid1': 'poster.mkv'},
                    'the channel image download would overwrite',
                )
            mock_th.schedule.assert_not_called()
            mock_signal.assert_not_called()
            self.assertTrue(old_path.exists())

    def test_a_late_download_meets_the_reserved_path_checks(self):
        # A collision specific to the late row (review pass 34 refuses
        # profile-wide ones such as {key}.nfo up front): it shares its
        # generated NFO and thumbnail with an earlier row's video stem.
        names = {'aaa': 'shared.mp4', 'late1': 'shared.webm'}
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=True, RENAME_SOURCES=[]),
            patch.object(
                Media, 'filename', property(lambda media: names[media.key]),
            ),
        ):
            source, first, first_path = self.make_downloaded(key='aaa')
            real_preflight = BackfillCommand._count_refused_media
            late_path = []

            def finish_a_download(command, downloaded, media_files):
                # Indexed and downloaded after the preflight read the
                # source's media (a row pending at preflight time is
                # checked there already, see review pass 31).
                late = Media.objects.create(key='late1', source=source, metadata=metadata)
                late_path.append(download_dummy_file(late))
                return real_preflight(command, downloaded, media_files)

            with patch.object(
                BackfillCommand, '_count_refused_media', finish_a_download,
            ):
                output, error = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            self.assertIsNotNone(error)
            self.assertIn('finished downloading during this run', output)
            self.assertIn('late1: not processed (reserved paths collide', output)
            self.assertIn('is also used by aaa', output)
            self.assertEqual(late_path[0].read_bytes(), b'fake-mkv-bytes')


class BackfillReviewFollowUp24TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Twenty-fourth review pass: a row recorded inside a sibling source's
        directory is refused, and an existing .jpg a rename would move onto
        a channel-image name counts with thumbnail copying off.
    '''

    def assert_refused(self, source, message):
        dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
        applied, exc = run_backfill_capture(
            '--source', str(source.uuid), '--apply',
        )
        for output, error in ((dry, dry_exc), (applied, exc)):
            self.assertIsNotNone(error)
            self.assertIn(message, output)
            self.assertIn('renamed: 0', output)
        self.assertEqual(summary_of(dry), summary_of(applied))

    def test_a_row_recorded_in_a_sibling_source_directory_is_refused(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, media, old_path = self.make_downloaded()
            sibling = make_bridge_source(
                key='UCsiblingabcdefghijklmno',
                name='acq-src-sibling',
                directory='acq-src-sibling',
            )
            sibling.make_directory()
            moved = sibling.directory_path / old_path.name
            old_path.rename(moved)
            media.media_file.name = str(
                moved.relative_to(media_file_storage.location)
            )
            media.save()
            sibling_file = moved.with_name(moved.stem + '.extra.mkv')
            sibling_file.write_bytes(b'sibling video')
            self.assert_refused(source, "recorded inside another source's directory")
            self.assertTrue(moved.exists())
            self.assertEqual(sibling_file.read_bytes(), b'sibling video')

    def test_a_moved_jpg_onto_a_channel_image_name_is_refused(self):
        overlay = (
            '{"*": {"media_format": "poster.{ext}", '
            '"copy_channel_images": true, "copy_thumbnails": false}}'
        )
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source, media, old_path = self.make_downloaded()
            old_jpg = old_path.with_suffix('.jpg')
            old_jpg.write_bytes(b'episode thumbnail')
            with (
                patch(f'{self.COMMAND}.TaskHistory') as mock_th,
                patch('sync.signals.download_source_images') as mock_signal,
            ):
                self.assert_refused(
                    source, 'the channel image download would overwrite',
                )
            mock_th.schedule.assert_not_called()
            mock_signal.assert_not_called()
            self.assertEqual(old_jpg.read_bytes(), b'episode thumbnail')
            source.refresh_from_db()
            self.assertFalse(source.copy_channel_images)  # never saved


class BackfillReviewFollowUp25TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Twenty-fifth review pass: a concurrent edit to a field the
        preflight relied on stops the save, a would-be target inside
        another source's directory is refused, and a symlinked cached
        thumbnail is never copied.
    '''

    def test_a_concurrent_path_field_edit_stops_the_save(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=True, RENAME_SOURCES=[]),
        ):
            source, media, old_path = self.make_downloaded()
            real_preflight = BackfillCommand._count_refused_media

            def edit_during_the_run(command, downloaded, media_files):
                Source.objects.filter(pk=source.pk).update(
                    source_resolution=Val(SourceResolution.VIDEO_720P),
                )
                return real_preflight(command, downloaded, media_files)

            with patch.object(
                BackfillCommand, '_count_refused_media', edit_during_the_run,
            ):
                output, error = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            self.assertIsNotNone(error)
            self.assertIn('source_resolution changed since this run read', output)
            self.assertIn('renamed: 0', output)
            self.assertTrue(old_path.exists())
            source.refresh_from_db()
            self.assertEqual(source.media_format, settings.MEDIA_FORMATSTR_DEFAULT)

    def test_a_target_inside_another_source_directory_is_refused(self):
        overlay = '{"*": {"write_nfo": true}}'
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source, media, old_path = self.make_downloaded()
            sibling = make_bridge_source(
                key='UCsiblingabcdefghijklmno',
                name='acq-src-sibling',
                directory='acq-src-sibling',
            )
            sibling.make_directory()
            # A stored format (not a validated overlay) reaching a sibling.
            Source.objects.filter(pk=source.pk).update(
                media_format='../acq-src-sibling/{key}.{ext}',
            )
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn("its target would be inside another source's", output)
                self.assertIn('renamed: 0', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            self.assertTrue(old_path.exists())
            self.assertEqual(list(sibling.directory_path.iterdir()), [])

    def test_a_symlinked_cached_thumbnail_is_not_copied(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            suffix = os.urandom(4).hex()
            real_name = f'thumbs/backfill-real-{suffix}.jpg'
            link_name = f'thumbs/backfill-link-{suffix}.jpg'
            real = Path(media.thumb.storage.path(real_name))
            link = Path(media.thumb.storage.path(link_name))
            real.parent.mkdir(parents=True, exist_ok=True)
            real.write_bytes(b'some other file')
            link.symlink_to(real)
            try:
                Media.objects.filter(pk=media.pk).update(
                    thumb=link_name, thumb_width=10, thumb_height=10,
                )
                with patch.object(Media, 'copy_thumbnail') as mock_copy:
                    dry = run_backfill('--source', str(source.uuid))
                    applied = run_backfill('--source', str(source.uuid), '--apply')
                mock_copy.assert_not_called()
                for output in (dry, applied):
                    self.assertIn('thumbs_copied: 0', output)
                    self.assertIn('is a symlink or not a regular file', output)
            finally:
                link.unlink()
                real.unlink()


class BackfillReviewFollowUp26TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Twenty-sixth review pass: another source's row recorded inside this
        source's tree refuses it, and a concurrent rename of the source
        (which {source} renders from) stops the save.
    '''

    def test_another_sources_row_in_this_tree_refuses_the_source(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, media, old_path = self.make_downloaded()
            sibling = make_bridge_source(
                key='UCsiblingabcdefghijklmno',
                name='acq-src-sibling',
                directory='acq-src-sibling',
            )
            sibling.make_directory()
            foreign = Media.objects.create(key='other', source=sibling, metadata=metadata)
            foreign_file = old_path.with_name(old_path.stem + '.extra.mkv')
            foreign_file.write_bytes(b'sibling video')
            foreign.media_file.name = str(
                foreign_file.relative_to(media_file_storage.location)
            )
            foreign.downloaded = True
            foreign.save()
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('recorded media overlap', output)
                self.assertIn(f'of {sibling}', output)
                self.assertIn('renamed: 0', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            self.assertTrue(old_path.exists())
            self.assertEqual(foreign_file.read_bytes(), b'sibling video')

    def test_a_concurrent_rename_of_the_source_stops_the_save(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=True, RENAME_SOURCES=[]),
        ):
            source, media, old_path = self.make_downloaded()
            real_preflight = BackfillCommand._count_refused_media

            def rename_during_the_run(command, downloaded, media_files):
                Source.objects.filter(pk=source.pk).update(name='tvshow')
                return real_preflight(command, downloaded, media_files)

            with patch.object(
                BackfillCommand, '_count_refused_media', rename_during_the_run,
            ):
                output, error = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            self.assertIsNotNone(error)
            self.assertIn('name changed since this run read the source', output)
            self.assertIn('renamed: 0', output)
            self.assertTrue(old_path.exists())


class BackfillReviewFollowUp27TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Twenty-seventh review pass: another source's row recorded through
        an alias that resolves into this tree refuses the source, and media
        still to be downloaded are reserved against channel-image names.
    '''

    def test_another_sources_row_through_an_alias_refuses_the_source(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, media, old_path = self.make_downloaded()
            sibling = make_bridge_source(
                key='UCsiblingabcdefghijklmno',
                name='acq-src-sibling',
                directory='acq-src-sibling',
            )
            sibling.make_directory()
            # An alias outside this source's tree, resolving into it.
            alias = source.directory_path.parent / 'alias-to-source'
            alias.symlink_to(source.directory_path, target_is_directory=True)
            physical = old_path.with_name(old_path.stem + '.extra.mkv')
            physical.write_bytes(b'sibling video')
            foreign = Media.objects.create(key='other', source=sibling, metadata=metadata)
            foreign.media_file.name = str(
                (alias / physical.name).relative_to(media_file_storage.location)
            )
            foreign.downloaded = True
            foreign.save()
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('recorded media overlap', output)
                self.assertIn('other of acq-src-sibling', output)
                self.assertIn('renamed: 0', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            self.assertEqual(physical.read_bytes(), b'sibling video')

    def test_a_pending_media_at_an_image_name_refuses_the_source(self):
        overlay = (
            '{"*": {"media_format": "poster.jpg", '
            '"copy_channel_images": true}}'
        )
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source = make_bridge_source()
            source.make_directory()
            pending = Media.objects.create(key='pend1', source=source, metadata=metadata)
            Media.objects.filter(pk=pending.pk).update(skip=False, manual_skip=False)
            with (
                patch(f'{self.COMMAND}.TaskHistory') as mock_th,
                patch('sync.signals.download_source_images') as mock_signal,
            ):
                dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
                applied, exc = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            mock_th.schedule.assert_not_called()
            mock_signal.assert_not_called()
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('(not downloaded yet)', output)
                self.assertIn('the channel image download would overwrite', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            source.refresh_from_db()
            self.assertFalse(source.copy_channel_images)  # never saved


class BackfillReviewFollowUp28TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Twenty-eighth review pass: every recorded video must lie inside its
        own source's directory, so neither a third directory shared with
        another source nor special entries beside a video outside the tree
        can be reached by the rename.
    '''

    def move_outside(self, media, old_path, directory_name):
        outside = old_path.parents[1] / directory_name
        outside.mkdir(exist_ok=True)
        moved = outside / old_path.name
        old_path.rename(moved)
        media.media_file.name = str(moved.relative_to(media_file_storage.location))
        media.save()
        return moved

    def assert_refused(self, source):
        dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
        applied, exc = run_backfill_capture(
            '--source', str(source.uuid), '--apply',
        )
        for output, error in ((dry, dry_exc), (applied, exc)):
            self.assertIsNotNone(error)
            self.assertIn("recorded outside the source's directory", output)
            self.assertIn('renamed: 0', output)
        self.assertEqual(summary_of(dry), summary_of(applied))

    def test_a_third_directory_shared_with_another_source_is_refused(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, media, old_path = self.make_downloaded()
            moved = self.move_outside(media, old_path, 'shared')
            sibling = make_bridge_source(
                key='UCsiblingabcdefghijklmno',
                name='acq-src-sibling',
                directory='acq-src-sibling',
            )
            foreign = Media.objects.create(key='other', source=sibling, metadata=metadata)
            foreign_file = moved.with_name(moved.stem + '.extra.mkv')
            foreign_file.write_bytes(b'sibling video')
            foreign.media_file.name = str(
                foreign_file.relative_to(media_file_storage.location)
            )
            foreign.downloaded = True
            foreign.save()
            self.assert_refused(source)
            self.assertTrue(moved.exists())
            self.assertEqual(foreign_file.read_bytes(), b'sibling video')

    def test_a_dangling_sidecar_beside_a_video_outside_the_tree_is_refused(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, media, old_path = self.make_downloaded()
            moved = self.move_outside(media, old_path, 'legacy')
            sidecar = moved.with_suffix('.en.srt')
            sidecar.symlink_to(moved.parent / 'missing.srt')
            self.assert_refused(source)
            self.assertTrue(moved.exists())
            self.assertTrue(sidecar.is_symlink())


class BackfillReviewFollowUp29TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Twenty-ninth review pass: media still to be downloaded meet every
        reserved-path check before the profile is saved, not only the
        channel-image one.
    '''

    def test_a_pending_media_whose_nfo_would_be_its_video_is_refused(self):
        overlay = '{"*": {"media_format": "{key}.nfo", "write_nfo": true}}'
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source = make_bridge_source()
            source.make_directory()
            pending = Media.objects.create(key='pend1', source=source, metadata=metadata)
            Media.objects.filter(pk=pending.pk).update(skip=False, manual_skip=False)
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('(not downloaded yet)', output)
                self.assertIn('its episode NFO would be the video file itself', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            source.refresh_from_db()
            self.assertFalse(source.write_nfo)  # the unsafe profile is not saved


class BackfillReviewFollowUp30TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Thirtieth review pass: a non-canonical target is refused before the
        rename, and two media (pending ones included) rendering to one
        video target refuse the source.
    '''

    def test_a_non_canonical_target_is_refused(self):
        overlay = '{"*": {"write_nfo": true}}'
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source, media, old_path = self.make_downloaded()
            (source.directory_path / 'sub').mkdir()
            Source.objects.filter(pk=source.pk).update(
                media_format='sub/../{key}.{ext}',
            )
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('is not canonical', output)
                self.assertIn('renamed: 0', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            self.assertTrue(old_path.exists())

    def test_two_pending_media_with_one_target_are_refused(self):
        overlay = (
            '{"*": {"media_format": "shared.{ext}", "write_nfo": false, '
            '"copy_thumbnails": false}}'
        )
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source = make_bridge_source()
            source.make_directory()
            for key in ('pend1', 'pend2'):
                pending = Media.objects.create(key=key, source=source, metadata=metadata)
                Media.objects.filter(pk=pending.pk).update(skip=False, manual_skip=False)
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('is also the target of', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            source.refresh_from_db()
            self.assertNotEqual(source.media_format, 'shared.{ext}')  # not saved


class BackfillReviewFollowUp31TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Thirty-first review pass: skipped rows that are not downloaded meet
        the reserved-path checks too, since they can become eligible later
        under the profile saved now.
    '''

    def test_a_skipped_pending_media_is_checked(self):
        overlay = '{"*": {"media_format": "{key}.nfo", "write_nfo": true}}'
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source = make_bridge_source()
            source.make_directory()
            skipped = Media.objects.create(key='skip1', source=source, metadata=metadata)
            Media.objects.filter(pk=skipped.pk).update(skip=True, manual_skip=True)
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('skip1', output)
                self.assertIn('its episode NFO would be the video file itself', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            source.refresh_from_db()
            self.assertFalse(source.write_nfo)  # the unsafe profile is not saved


class BackfillReviewFollowUp32TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Thirty-second review pass: two rows recording one file refuse the
        source, and so does a target whose directory cannot be created.
    '''

    def assert_refused(self, source, message):
        dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
        applied, exc = run_backfill_capture(
            '--source', str(source.uuid), '--apply',
        )
        for output, error in ((dry, dry_exc), (applied, exc)):
            self.assertIsNotNone(error)
            self.assertIn(message, output)
            self.assertIn('renamed: 0', output)
        self.assertEqual(summary_of(dry), summary_of(applied))

    def test_two_rows_recording_one_file_refuse_the_source(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, first, old_path = self.make_downloaded(key='aaa')
            second = Media.objects.create(key='bbb', source=source, metadata=metadata)
            second.media_file.name = first.media_file.name
            second.downloaded = True
            second.save()
            self.assert_refused(source, 'is also recorded for')
            self.assertTrue(old_path.exists())

    def test_a_target_under_a_regular_file_is_refused(self):
        overlay = '{"*": {"media_format": "occupied/{key}.{ext}"}}'
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source, media, old_path = self.make_downloaded()
            (source.directory_path / 'occupied').write_bytes(b'a regular file')
            self.assert_refused(source, 'cannot be created')
            self.assertTrue(old_path.exists())
            source.refresh_from_db()
            self.assertNotEqual(source.media_format, 'occupied/{key}.{ext}')


class BackfillReviewFollowUp33TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Thirty-third review pass: a sidecar move landing on the renamed
        video is refused, and a dry-run treats a path an earlier rename
        vacates as free, as apply finds it.
    '''

    def test_a_sidecar_move_onto_the_renamed_video_is_refused(self):
        overlay = '{"*": {"media_format": "{key}.nfo", "write_nfo": false}}'
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source, media, old_path = self.make_downloaded()
            old_nfo = old_path.with_suffix('.nfo')
            old_nfo.write_text('<episodedetails><id>vid1</id></episodedetails>')
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('lands on the renamed video itself', output)
                self.assertIn('renamed: 0', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            self.assertEqual(old_path.read_bytes(), b'fake-mkv-bytes')

    def test_a_dry_run_frees_a_path_an_earlier_rename_vacates(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, first, first_path = self.make_downloaded(key='aaa')
            _, second, second_path = self.make_downloaded(key='bbb', source=source)
            names = {'aaa': 'moved-away.mkv', 'bbb': first_path.name}
            with patch.object(
                Media, 'filename', property(lambda media: names[media.key]),
            ):
                dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
                applied, exc = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            self.assertIsNone(dry_exc, dry)
            self.assertIsNone(exc, applied)
            for output in (dry, applied):
                self.assertIn('renamed: 2', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            second.refresh_from_db()
            self.assertEqual(Path(second.media_file.path), first_path)


class BackfillReviewFollowUp34TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Thirty-fourth review pass: a destructive profile is refused even for
        a source with no media, and an in-place video's existing sidecars
        count against channel-image names.
    '''

    def test_a_destructive_profile_is_refused_without_any_media(self):
        overlay = '{"*": {"media_format": "{key}.nfo", "write_nfo": true}}'
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source = make_bridge_source()
            source.make_directory()
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('the profile: media_format', output)
                self.assertIn('each episode NFO would replace its video', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            source.refresh_from_db()
            self.assertFalse(source.write_nfo)  # never saved

    def test_an_in_place_videos_sidecar_at_an_image_name_is_refused(self):
        overlay = (
            '{"*": {"media_format": "poster.{ext}", '
            '"copy_channel_images": true, "copy_thumbnails": false}}'
        )
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source, media, old_path = self.make_downloaded()
            placed = source.directory_path / 'poster.mkv'
            old_path.rename(placed)
            media.media_file.name = str(placed.relative_to(media_file_storage.location))
            media.save()
            thumb = source.directory_path / 'poster.jpg'
            thumb.write_bytes(b'episode thumbnail')
            with (
                patch(f'{self.COMMAND}.TaskHistory') as mock_th,
                patch('sync.signals.download_source_images') as mock_signal,
            ):
                dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
                applied, exc = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            mock_th.schedule.assert_not_called()
            mock_signal.assert_not_called()
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn(f'the channel image download would overwrite {thumb}', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            self.assertEqual(thumb.read_bytes(), b'episode thumbnail')


class BackfillReviewFollowUp35TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Thirty-fifth review pass: a dry-run's old-stem glob sees the files
        earlier renames of the run put there, as apply's does.
    '''

    def test_a_projected_video_counts_in_a_later_stem_glob(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, first, first_path = self.make_downloaded(key='aaa')
            _, second, second_path = self.make_downloaded(key='bbb', source=source)
            # bbb already sits at foo.mkv; aaa's rename puts its video at
            # foo.webm, which bbb's old-stem glob then takes.
            foo = source.directory_path / 'foo.mkv'
            second_path.rename(foo)
            second.media_file.name = str(foo.relative_to(media_file_storage.location))
            second.save()
            names = {'aaa': 'foo.webm', 'bbb': 'bar.mkv'}
            with patch.object(
                Media, 'filename', property(lambda media: names[media.key]),
            ):
                dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
                applied, exc = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('other media files would be moved with it', output)
                self.assertIn('renamed: 1', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            first.refresh_from_db()
            self.assertEqual(
                Path(first.media_file.path), source.directory_path / 'foo.webm',
            )
            self.assertTrue((source.directory_path / 'foo.webm').exists())


class BackfillReviewFollowUp36TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Thirty-sixth review pass: the destinations of existing sidecar moves
        are claimed across media whatever the options, so another media's
        generated NFO cannot land on one.
    '''

    def test_a_moved_sidecar_and_another_medias_nfo_collide(self):
        overlay = '{"*": {"write_nfo": true, "copy_channel_images": false}}'
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source, first, first_path = self.make_downloaded(key='aaa')
            self.make_downloaded(key='bbb', source=source)
            # aaa's old.bar.nfo moves to foo.bar.nfo, the NFO bbb's
            # foo.bar.webm target would generate.
            moved_nfo = first_path.with_name(first_path.stem + '.bar.nfo')
            moved_nfo.write_text('<episodedetails><id>aaa</id></episodedetails>')
            names = {'aaa': 'foo.mkv', 'bbb': 'foo.bar.webm'}
            with patch.object(
                Media, 'filename', property(lambda media: names[media.key]),
            ):
                dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
                applied, exc = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('is also used by aaa', output)
                self.assertIn('renamed: 0', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            self.assertTrue(moved_nfo.exists())


class BackfillReviewFollowUp37TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Thirty-seventh review pass: a pending media's NFO path holding a
        foreign file refuses the source, and turning write_nfo on counts a
        running download as in flight.
    '''

    make_busy_source = BackfillReviewFollowUp4TestCase.make_busy_source
    locked = BackfillReviewFollowUp4TestCase.locked

    def test_a_foreign_file_at_a_pending_medias_nfo_path_is_refused(self):
        names = {'pend1': 'pending.mkv'}
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.object(
                Media, 'filename', property(lambda media: names[media.key]),
            ),
        ):
            source = make_bridge_source()
            source.make_directory()
            pending = Media.objects.create(key='pend1', source=source, metadata=metadata)
            Media.objects.filter(pk=pending.pk).update(skip=False, manual_skip=False)
            foreign = source.directory_path / 'pending.nfo'
            foreign.write_text('<episodedetails><title>Mine</title></episodedetails>')
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('holds a file that is not its own NFO', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            self.assertIn('Mine', foreign.read_text())
            source.refresh_from_db()
            self.assertFalse(source.write_nfo)  # never saved

    def test_turning_write_nfo_on_counts_a_running_download(self):
        overlay = '{"*": {"write_nfo": true}}'
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source, busy = self.make_busy_source()
            with self.locked(busy):
                output, error = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            self.assertIsNotNone(error)
            self.assertIn(f'IN FLIGHT: {busy}', output)
            self.assertIn('in_flight: 1', output)


class BackfillReviewFollowUp38TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Thirty-eighth review pass: a pending media's video target or
        thumbnail path already taken by a file refuses the source.
    '''

    def assert_refused_for(self, existing_name, message):
        names = {'pend1': 'pending.mkv'}
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.object(
                Media, 'filename', property(lambda media: names[media.key]),
            ),
        ):
            source = make_bridge_source()
            source.make_directory()
            pending = Media.objects.create(key='pend1', source=source, metadata=metadata)
            Media.objects.filter(pk=pending.pk).update(skip=False, manual_skip=False)
            existing = source.directory_path / existing_name
            existing.write_bytes(b'someone else')
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn(message, output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            self.assertEqual(existing.read_bytes(), b'someone else')
            source.refresh_from_db()
            self.assertFalse(source.write_nfo)  # never saved

    def test_an_existing_file_at_a_pending_target_is_refused(self):
        self.assert_refused_for('pending.mkv', 'its target')

    def test_an_existing_file_at_a_pending_thumbnail_path_is_refused(self):
        self.assert_refused_for('pending.jpg', 'its thumbnail path')


class BackfillReviewFollowUp39TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Thirty-ninth review pass: a thumbnail rendered on the video itself
        is refused (per media and for the profile), and a source directory
        outside the download root is refused before any save.
    '''

    def test_a_jpg_video_profile_is_refused_without_media(self):
        overlay = '{"*": {"media_format": "{key}.jpg", "copy_thumbnails": true}}'
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source = make_bridge_source()
            source.make_directory()
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('gives every video a .jpg extension', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            source.refresh_from_db()
            self.assertFalse(source.copy_thumbnails)  # never saved

    def test_a_thumbnail_on_the_video_itself_is_refused(self):
        names = {'vid1': 'movie.jpg'}
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, media, old_path = self.make_downloaded()
            with patch.object(
                Media, 'filename', property(lambda media: names[media.key]),
            ):
                dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
                applied, exc = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('its thumbnail would be the video file itself', output)
                self.assertIn('renamed: 0', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            self.assertEqual(old_path.read_bytes(), b'fake-mkv-bytes')

    def test_a_source_directory_outside_the_root_is_refused_before_saving(self):
        with temp_download_root(), tempfile.TemporaryDirectory() as outside:
            elsewhere = Path(outside) / 'legacy-source'
            source = make_bridge_source(directory=str(elsewhere))
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('is outside', output)
                self.assertIn('errors: 1', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            self.assertFalse(elsewhere.exists())
            source.refresh_from_db()
            self.assertFalse(source.copy_channel_images)  # never saved


class BackfillReviewFollowUp40TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Fortieth review pass: the profile-level check reads a rendered
        example, so a format spec that changes the suffix cannot hide a
        .nfo or .jpg video extension.
    '''

    def assert_profile_refused(self, overlay, message):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source = make_bridge_source()
            source.make_directory()
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn(message, output)
            self.assertEqual(summary_of(dry), summary_of(applied))

    def test_a_format_spec_rendering_nfo_is_refused(self):
        self.assert_profile_refused(
            '{"*": {"media_format": "{key}.{ext:.0}nfo", "write_nfo": true}}',
            'gives every video a .nfo extension',
        )

    def test_a_format_spec_rendering_jpg_is_refused(self):
        self.assert_profile_refused(
            '{"*": {"media_format": "{key}.{ext:.0}jpg", "copy_thumbnails": true}}',
            'gives every video a .jpg extension',
        )


class BackfillReviewFollowUp41TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Forty-first review pass: with sidecars or channel images on, a
        format without a fixed extension is refused, since a media's own
        data (a title ending in ".jpg") could supply the suffix.
    '''

    def test_a_format_without_a_fixed_extension_is_refused(self):
        overlay = '{"*": {"media_format": "{title_full}", "copy_thumbnails": true}}'
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source = make_bridge_source()
            source.make_directory()
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('does not end in a fixed extension', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            source.refresh_from_db()
            self.assertFalse(source.copy_thumbnails)  # never saved


class BackfillReviewFollowUp42TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Forty-second review pass: a stem taken from media data that could
        name a video after a channel image (or tvshow) is refused, and an
        image-queueing save waits for running downloads with the cascade
        off.
    '''

    make_busy_source = BackfillReviewFollowUp4TestCase.make_busy_source
    locked = BackfillReviewFollowUp4TestCase.locked

    def assert_profile_refused(self, overlay, message):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source = make_bridge_source()
            source.make_directory()
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn(message, output)
            self.assertEqual(summary_of(dry), summary_of(applied))

    def test_a_data_stem_that_can_be_a_channel_image_is_refused(self):
        self.assert_profile_refused(
            '{"*": {"media_format": "{title_full}.jpg", '
            '"copy_channel_images": true}}',
            'can name a video after a channel image',
        )

    def test_a_data_stem_that_can_be_tvshow_is_refused(self):
        self.assert_profile_refused(
            '{"*": {"media_format": "{title_full}.{ext}", "write_nfo": true}}',
            'can name a video "tvshow"',
        )

    def test_a_shortened_key_stem_is_refused(self):
        self.assert_profile_refused(
            '{"*": {"media_format": "{key:.6}.mkv", "write_nfo": true}}',
            'can name a video "tvshow"',
        )

    def test_a_nested_format_spec_stem_is_refused(self):
        self.assert_profile_refused(
            '{"*": {"media_format": "{title_full:.{video_order}}.mkv", '
            '"write_nfo": true}}',
            'can name a video "tvshow"',
        )

    def test_a_key_stem_is_not_refused(self):
        overlay = (
            '{"*": {"media_format": "{key}.{ext}", "write_nfo": true, '
            '"copy_channel_images": true, "copy_thumbnails": true}}'
        )
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
            patch(f'{self.COMMAND}.TaskHistory'),
        ):
            source = make_bridge_source()
            source.make_directory()
            output = run_backfill('--source', str(source.uuid))
            self.assertNotIn('can name a video', output)

    def test_an_image_queueing_save_waits_for_running_downloads(self):
        overlay = '{"*": {"copy_channel_images": true}}'
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
            patch('sync.signals.download_source_images') as mock_signal,
        ):
            source, busy = self.make_busy_source()
            with self.locked(busy):
                dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
                applied, exc = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            mock_signal.assert_not_called()
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn(f'IN FLIGHT: {busy}', output)
                self.assertIn('in_flight: 1', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            source.refresh_from_db()
            self.assertFalse(source.copy_channel_images)  # never saved


class BackfillReviewFollowUp45TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Forty-fifth and forty-sixth review passes: a dry-run's {key} sweep
        sees the files earlier renames of the run put there, and a
        directory segment or name that media data can empty, climb out of
        (".."), or split ("/") counts as reaching the source directory.
    '''

    assert_profile_refused = BackfillReviewFollowUp42TestCase.assert_profile_refused

    def test_a_projected_video_counts_in_a_later_key_sweep(self):
        overlay = '{"*": {"media_format": "{key}.{ext}"}}'
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source, first, _ = self.make_downloaded(key='aaa')
            self.make_downloaded(key='bbb', source=source)
            # aaa's new name carries bbb's key, so bbb's {key} sweep would
            # take aaa's freshly moved video.
            names = {'aaa': 'foo-bbb.mkv', 'bbb': 'bar.mkv'}
            with patch.object(
                Media, 'filename', property(lambda media: names[media.key]),
            ):
                dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
                applied, exc = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('bbb: not renamed', output)
                self.assertIn(str(source.directory_path / 'foo-bbb.mkv'), output)
                self.assertIn('renamed: 1', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            first.refresh_from_db()
            self.assertEqual(
                Path(first.media_file.path), source.directory_path / 'foo-bbb.mkv',
            )

    def test_a_directory_that_can_render_empty_is_the_source_directory(self):
        self.assert_profile_refused(
            '{"*": {"media_format": "{uploader}/poster.jpg", '
            '"copy_channel_images": true}}',
            'at the channel image poster.jpg',
        )

    def test_a_title_directory_that_can_climb_is_the_source_directory(self):
        # A title of ".." takes "fixed/../poster.jpg" to the source root.
        self.assert_profile_refused(
            '{"*": {"media_format": "fixed/{title_full}/poster.jpg", '
            '"copy_channel_images": true}}',
            'at the channel image poster.jpg',
        )

    def test_a_literal_parent_segment_is_resolved(self):
        overlay = '{"*": {"copy_channel_images": true}}'
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
        ):
            source = make_bridge_source()
            source.make_directory()
            # A stored format (not a validated overlay): "fixed/.." is the
            # source directory itself.
            Source.objects.filter(pk=source.pk).update(
                media_format='fixed/../poster.jpg',
            )
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn('at the channel image poster.jpg', output)
            self.assertEqual(summary_of(dry), summary_of(applied))

    def test_a_slash_capable_field_in_the_stem_frees_the_name(self):
        # An uploader of "/../../poster" makes the name poster.jpg in the
        # source directory, whatever literal text precedes it.
        self.assert_profile_refused(
            '{"*": {"media_format": "fixed/s{uploader}.jpg", '
            '"copy_channel_images": true}}',
            'can name a video after a channel image',
        )

    def test_a_literal_directory_is_not_the_source_directory(self):
        overlay = (
            '{"*": {"media_format": "Channel/{key}/poster.jpg", '
            '"copy_channel_images": true}}'
        )
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
            patch(f'{self.COMMAND}.TaskHistory'),
        ):
            source = make_bridge_source()
            source.make_directory()
            output = run_backfill('--source', str(source.uuid))
            self.assertNotIn('at the channel image', output)
            self.assertNotIn('can name a video', output)


class BackfillReviewFollowUp47TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Forty-seventh review pass: a download finishing under its lock
        holds back a path-changing save, a title-derived directory that
        can climb out of the source is refused, and the stray-sidecar scan
        decides alike in both modes after earlier renames.
    '''

    assert_profile_refused = BackfillReviewFollowUp42TestCase.assert_profile_refused
    locked = BackfillReviewFollowUp4TestCase.locked

    def test_a_finishing_download_holds_back_an_image_queueing_save(self):
        overlay = '{"*": {"copy_channel_images": true}}'
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
            patch('sync.signals.download_source_images') as mock_signal,
        ):
            # Saved as downloaded, still under its lock: its sidecars are
            # still being written under the old source.
            source, busy, _ = self.make_downloaded(key='busy1')
            with self.locked(busy):
                dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
                applied, exc = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            mock_signal.assert_not_called()
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn(f'IN FLIGHT: {busy}', output)
                self.assertIn('in_flight: 1', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            source.refresh_from_db()
            self.assertFalse(source.copy_channel_images)  # never saved

    def test_a_title_directory_that_can_climb_out_is_refused(self):
        # A title of ".." renders "../../<key>.mkv".
        self.assert_profile_refused(
            '{"*": {"media_format": "{title_full}/{title_full}/{key}.{ext}"}}',
            'can put a video above the source directory',
        )

    def test_an_uncleaned_field_is_refused(self):
        self.assert_profile_refused(
            '{"*": {"media_format": "Channel/{uploader} [{key}].{ext}"}}',
            'uploader can hold "/" and ".." segments',
        )

    def put_in_place(self, media, name):
        path = Path(media.media_file.path).with_name(name)
        Path(media.media_file.path).rename(path)
        media.media_file.name = str(path.relative_to(media_file_storage.location))
        media.save()
        return path

    def run_both_with_names(self, source, names):
        with patch.object(
            Media, 'filename', property(lambda media: names[media.key]),
        ):
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
        return (dry, dry_exc), (applied, exc)

    def test_an_earlier_video_renamed_onto_a_later_key_is_not_a_stray(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, _first, _ = self.make_downloaded(key='aaa')
            _, second, _ = self.make_downloaded(key='bbb', source=source)
            self.put_in_place(second, 'bar.mkv')
            runs = self.run_both_with_names(
                source, {'aaa': 'foo-bbb.mkv', 'bbb': 'bar.mkv'},
            )
            for output, _error in runs:
                self.assertNotIn('leftover sidecar', output)
                self.assertIn('renamed: 1', output)
                self.assertIn('already_in_place: 1', output)
            self.assertEqual(summary_of(runs[0][0]), summary_of(runs[1][0]))

    def test_a_key_match_an_earlier_rename_moves_away_is_not_a_stray(self):
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
        ):
            source, _first, _ = self.make_downloaded(key='aaa')
            _, second, _ = self.make_downloaded(key='bbb', source=source)
            self.put_in_place(second, 'bar.mkv')
            # aaa's {key} sweep moves this away; it carries bbb's key too.
            notes = source.directory_path / 'notes-aaa-bbb.txt'
            notes.write_bytes(b'notes')
            runs = self.run_both_with_names(
                source, {'aaa': 'foo.mkv', 'bbb': 'bar.mkv'},
            )
            for output, _error in runs:
                self.assertNotIn('leftover sidecar', output)
                self.assertIn('renamed: 1', output)
                self.assertIn('already_in_place: 1', output)
                self.assertIn('key_matched_moves: 1', output)
            self.assertEqual(summary_of(runs[0][0]), summary_of(runs[1][0]))
            self.assertFalse(notes.exists())


class BackfillReviewFollowUp48TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Forty-eighth review pass: a format spec or an empty stream field
        that can make a directory segment "..", or put "/" into a path, is
        refused, and so is a format that cannot tell media apart.
    '''

    assert_profile_refused = BackfillReviewFollowUp42TestCase.assert_profile_refused

    def test_a_format_spec_that_pads_a_directory_into_dots_is_refused(self):
        # hdr is "" for a non-HDR download: each segment renders "..".
        self.assert_profile_refused(
            '{"*": {"media_format": '
            '"fixed/{hdr:.^2}/{hdr:.^2}/{hdr:.^2}/{key}.{ext}"}}',
            'can put a video above the source directory',
        )

    def test_an_empty_stream_field_between_dots_is_refused(self):
        self.assert_profile_refused(
            '{"*": {"media_format": "fixed/.{hdr}./{key}.{ext}"}}',
            'can put a video above the source directory',
        )

    def test_a_slash_fill_is_refused(self):
        self.assert_profile_refused(
            '{"*": {"media_format": "{hdr:/^3}{key}.{ext}"}}',
            'hdr can hold "/" and ".." segments',
        )

    def test_a_format_that_cannot_tell_media_apart_is_refused(self):
        self.assert_profile_refused(
            '{"*": {"media_format": "shared.{ext}", "write_nfo": false, '
            '"copy_thumbnails": false, "copy_channel_images": false}}',
            'does not use the whole {key}',
        )

    def test_a_stream_field_directory_is_not_refused(self):
        overlay = '{"*": {"media_format": "{resolution}/{key}.{ext}"}}'
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
            patch(f'{self.COMMAND}.TaskHistory'),
        ):
            source = make_bridge_source()
            source.make_directory()
            output = run_backfill('--source', str(source.uuid))
            self.assertNotIn('above the source directory', output)
            self.assertNotIn('tells media apart', output)


class BackfillReviewFollowUp49TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Forty-ninth review pass: the source fields are checked with their
        real values, and a truncated field does not tell media apart.
    '''

    assert_profile_refused = BackfillReviewFollowUp42TestCase.assert_profile_refused

    def run_with_stored_format(self, media_format, **source_fields):
        # An overlay that changes a field, so the source would be saved.
        overlay = '{"*": {"copy_channel_images": true}}'
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
            patch(f'{self.COMMAND}.TaskHistory'),
        ):
            source = make_bridge_source(**source_fields)
            source.make_directory()
            # A stored format (not a validated overlay).
            Source.objects.filter(pk=source.pk).update(media_format=media_format)
            dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
            applied, exc = run_backfill_capture(
                '--source', str(source.uuid), '--apply',
            )
            self.assertEqual(summary_of(dry), summary_of(applied))
            return (dry, dry_exc), (applied, exc)

    def test_a_source_named_dot_dot_cannot_climb_out(self):
        for output, error in self.run_with_stored_format(
            '{source_full}/{source_full}/{key}.{ext}', name='..',
        ):
            self.assertIsNotNone(error)
            self.assertIn('can put a video above the source directory', output)

    def test_a_source_full_directory_is_not_refused(self):
        for output, _error in self.run_with_stored_format(
            '{source_full}/{key}.{ext}',
        ):
            self.assertNotIn('above the source directory', output)

    def test_a_truncated_key_does_not_tell_media_apart(self):
        self.assert_profile_refused(
            '{"*": {"media_format": "{key:.1}.{ext}", "write_nfo": false, '
            '"copy_thumbnails": false, "copy_channel_images": false}}',
            'does not use the whole {key}',
        )


class BackfillReviewFollowUp50TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Fiftieth review pass: an indexed field does not tell media apart.
    '''

    assert_profile_refused = BackfillReviewFollowUp42TestCase.assert_profile_refused

    def test_an_indexed_key_does_not_tell_media_apart(self):
        self.assert_profile_refused(
            '{"*": {"media_format": "{key[0]}.{ext}", "write_nfo": false, '
            '"copy_thumbnails": false, "copy_channel_images": false}}',
            'does not use the whole {key}',
        )


class BackfillReviewFollowUp51TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Fifty-first review pass: only the whole {key} tells every media
        apart.
    '''

    assert_profile_refused = BackfillReviewFollowUp42TestCase.assert_profile_refused

    def test_a_date_does_not_tell_media_apart(self):
        self.assert_profile_refused(
            '{"*": {"media_format": "{yyyy}.{ext}", "write_nfo": false, '
            '"copy_thumbnails": false, "copy_channel_images": false}}',
            'does not use the whole {key}',
        )

    def test_a_padded_key_tells_media_apart(self):
        overlay = '{"*": {"media_format": "{key:_>12}.{ext}"}}'
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
            patch(f'{self.COMMAND}.TaskHistory'),
        ):
            source = make_bridge_source()
            source.make_directory()
            output = run_backfill('--source', str(source.uuid))
            self.assertNotIn('whole {key}', output)


class BackfillReviewFollowUp52TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Fifty-second review pass: a nested field in a format spec can
        supply a "/" fill.
    '''

    assert_profile_refused = BackfillReviewFollowUp42TestCase.assert_profile_refused

    def test_a_nested_fill_field_is_refused(self):
        # An uploader starting with "/" makes the empty hdr render "/".
        self.assert_profile_refused(
            '{"*": {"media_format": '
            '"..{hdr:{uploader[0]}>1}..{hdr:{uploader[0]}>1}..{key}.mkv"}}',
            'can put a video above the source directory',
        )


class BackfillReviewFollowUp53TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Fifty-third review pass: the whole {key} must stay in the path
        once ".." segments are resolved.
    '''

    run_with_stored_format = BackfillReviewFollowUp49TestCase.run_with_stored_format

    def test_a_key_directory_a_parent_segment_drops_is_refused(self):
        for output, error in self.run_with_stored_format('{key}/../shared.{ext}'):
            self.assertIsNotNone(error)
            self.assertIn('does not use the whole {key}', output)

    def test_a_key_directory_that_stays_is_not_refused(self):
        for output, _error in self.run_with_stored_format('{key}/shared.{ext}'):
            self.assertNotIn('whole {key}', output)


class BackfillReviewFollowUp54TestCase(BackfillFollowUpMixin, TestCase):
    '''
        Fifty-fourth review pass: the run's own channel-image enqueue
        holds back a path-changing save while a download is running, and
        literal text cannot pass for a field marker.
    '''

    make_busy_source = BackfillReviewFollowUp4TestCase.make_busy_source
    locked = BackfillReviewFollowUp4TestCase.locked
    run_with_stored_format = BackfillReviewFollowUp49TestCase.run_with_stored_format

    def test_a_pending_image_enqueue_waits_for_running_downloads(self):
        # copy_channel_images is already on and poster.jpg is missing, so
        # this run queues the image job itself after saving the format.
        overlay = (
            '{"*": {"media_format": "{key}.{ext}", "copy_channel_images": true}}'
        )
        with (
            temp_download_root(),
            override_settings(RENAME_ALL_SOURCES=False, RENAME_SOURCES=[]),
            patch.dict('os.environ', {'MEDIANEST_BRIDGE_SOURCE_DEFAULTS': overlay}),
            patch(f'{self.COMMAND}.TaskHistory') as task_history,
        ):
            source, busy = self.make_busy_source()
            Source.objects.filter(pk=source.pk).update(copy_channel_images=True)
            old_format = Source.objects.get(pk=source.pk).media_format
            with self.locked(busy):
                dry, dry_exc = run_backfill_capture('--source', str(source.uuid))
                applied, exc = run_backfill_capture(
                    '--source', str(source.uuid), '--apply',
                )
            task_history.schedule.assert_not_called()
            for output, error in ((dry, dry_exc), (applied, exc)):
                self.assertIsNotNone(error)
                self.assertIn(f'IN FLIGHT: {busy}', output)
                self.assertIn('in_flight: 1', output)
            self.assertEqual(summary_of(dry), summary_of(applied))
            source.refresh_from_db()
            self.assertEqual(source.media_format, old_format)  # never saved

    def test_literal_text_cannot_pass_for_the_key(self):
        for output, error in self.run_with_stored_format('shared\x03.{ext}'):
            self.assertIsNotNone(error)
            self.assertIn('does not use the whole {key}', output)
