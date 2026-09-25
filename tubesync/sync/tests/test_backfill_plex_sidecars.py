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
import tempfile
from contextlib import contextmanager
from io import StringIO
from unittest.mock import PropertyMock, patch
from xml.etree import ElementTree

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from huey.exceptions import TaskLockedException

from medianest_bridge.config import source_defaults
from sync.choices import (
    Val, Fallback, SourceResolution,
    YouTube_AudioCodec, YouTube_VideoCodec,
    YouTube_SourceType,
)
from sync.forms import SourceForm
from sync.models import Media, Source
from sync.models._migrations import media_file_storage

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


class BackfillPlexSidecarsTestCase(TestCase):

    def setUp(self):
        logging.disable(logging.CRITICAL)

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
            # Playlist NFO numbering is the legacy scheme, unchanged by T1/T2.
            self.assertEqual(playlist_tree.find('season').text, '1')
            self.assertEqual(playlist_tree.find('episode').text, '1')

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
            real_save = SourceForm.save
            calls = []

            def save_once_then_fail(form, *args, **kwargs):
                calls.append(form.instance.pk)
                if len(calls) == 1:
                    raise RuntimeError('db down')
                return real_save(form, *args, **kwargs)

            with (
                patch.object(SourceForm, 'save', save_once_then_fail),
                self.assertRaises(CommandError),
            ):
                run_backfill('--all-bridge-sources', '--apply')
            self.assertEqual(len(calls), 2)

    def test_apply_refuses_to_run_as_a_different_user(self):
        with temp_download_root():
            source, media, old_path = self.make_downloaded()
            real_uid = __import__('os').geteuid()
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
