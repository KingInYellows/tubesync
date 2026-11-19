'''
    MED-702: artist-first episode titles and the show title override (sync/plex_naming.py, medianest_plex_naming).

    All filesystem writes go to a temporary DOWNLOAD_ROOT, never the real
    downloads folder.
'''
import json
import logging
import tempfile
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from huey.exceptions import TaskLockedException

from sync.choices import (
    Val, Fallback, SourceResolution,
    YouTube_AudioCodec, YouTube_VideoCodec, YouTube_SourceType,
)
from sync.management.commands.medianest_plex_naming import (
    artist_media_format, hand_owned_tvshow_nfo,
)
from sync.models import Media, Source
from sync.models._migrations import media_file_storage
from sync.plex_naming import artist_title
from sync.tvshow_nfo import (
    _clear_show_title_cache, resolve_show_title, write_tvshow_nfo,
)

from .fixtures import all_test_metadata

# title 'no fancy stuff title', upload_date 2017-09-11
BORING = all_test_metadata['boring']
BRIDGE_FORMAT = (
    'Season {episode_yyyy}/'
    's{episode_yyyy}e{episode_mmddnn} - {title_full_bounded} [{key}].{ext}'
)


def metadata_with(**fields):
    data = json.loads(BORING)
    data.update(fields)
    return json.dumps(data)


@contextmanager
def temp_download_root():
    with tempfile.TemporaryDirectory() as tmp_dir:
        with (
            override_settings(DOWNLOAD_ROOT=tmp_dir),
            patch.object(media_file_storage, 'location', tmp_dir),
        ):
            yield tmp_dir


def make_source(**overrides):
    defaults = dict(
        source_type=Val(YouTube_SourceType.PLAYLIST),
        key='PLabcdefghijklmnopqrstuv',
        name='acq-src-PLabcdefghijklmnopqrstuv',
        directory='acq-src-PLabcdefghijklmnopqrstuv',
        media_format=BRIDGE_FORMAT,
        write_nfo=True,
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
    path = media.filepath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'fake-mkv-bytes')
    media.media_file.name = str(path.relative_to(media_file_storage.location))
    media.downloaded = True
    media.save()
    media.write_nfo_file()
    return path


def run(*args):
    out = StringIO()
    call_command('medianest_plex_naming', *args, stdout=out)
    return out.getvalue()


class ArtistTitleTestCase(SimpleTestCase):

    def test_titles_from_the_live_music_playlist(self):
        cases = (
            # (title, artists, expected)
            ('Hans Zimmer | Live in Concert – The Ultimate Experience', [],
             'Hans Zimmer - Live in Concert – The Ultimate Experience'),
            ('Live in Cologne', ['Hans Zimmer'], 'Hans Zimmer - Live in Cologne'),
            ('Alice In Chains - Nutshell (MTV Unplugged - HD Video)', [],
             'Alice In Chains - Nutshell (MTV Unplugged - HD Video)'),
            ('Alice In Chains - Nutshell (MTV Unplugged - HD Video)', ['Alice in Chains'],
             'Alice In Chains - Nutshell (MTV Unplugged - HD Video)'),
            ('Jørgen Dahl Moe | Dancing In the Dark | The Voice Norway', [],
             'Jørgen Dahl Moe - Dancing In the Dark | The Voice Norway'),
            ('Alicia Keys & John Mayer - If I ain\'t got you', ['Alicia Keys', 'John Mayer'],
             'Alicia Keys & John Mayer - If I ain\'t got you'),
            ('Bon Iver at AIR Studios (4AD/Jagjaguwar Session)', ['Bon Iver'],
             'Bon Iver at AIR Studios (4AD/Jagjaguwar Session)'),
            # no artist credit and no separator: left alone, the channel
            # (a venue or TV show) is not used as the artist
            ('Most Spectacular Covers in The Voice History', [],
             'Most Spectacular Covers in The Voice History'),
            ('Nutshell', ['Alice In Chains', 'Layne Staley'],
             'Alice In Chains, Layne Staley - Nutshell'),
        )
        for title, artists, expected in cases:
            with self.subTest(title=title):
                self.assertEqual(artist_title(title, artists), expected)

    def test_artist_media_format_swaps_only_the_title_key(self):
        self.assertEqual(
            artist_media_format(BRIDGE_FORMAT),
            BRIDGE_FORMAT.replace('{title_full_bounded}', '{artist_title_bounded}'),
        )
        self.assertEqual(
            artist_media_format('{yyyy_mm_dd}_{title_full}_{key}.{ext}'),
            '{yyyy_mm_dd}_{artist_title}_{key}.{ext}',
        )
        swapped = artist_media_format(BRIDGE_FORMAT)
        self.assertEqual(artist_media_format(swapped), swapped)
        self.assertIsNone(artist_media_format('{title}_{key}.{ext}'))


class ArtistTitleMediaTestCase(TestCase):

    def setUp(self):
        logging.disable(logging.CRITICAL)
        _clear_show_title_cache()

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_other_sources_keep_their_names_and_nfo_titles(self):
        with temp_download_root():
            source = make_source()
            media = Media.objects.create(
                key='vid1', source=source,
                metadata=metadata_with(artists=['Some Artist']),
            )
            self.assertEqual(
                media.filepath.relative_to(source.directory_path).as_posix(),
                'Season 2017/s2017e091101 - no fancy stuff title [vid1].mkv',
            )
            nfo = ElementTree.fromstring(media.nfoxml)
            self.assertEqual(nfo.find('title').text, 'no fancy stuff title')

    def test_artist_format_names_file_and_nfo_alike(self):
        with temp_download_root():
            source = make_source(media_format=artist_media_format(BRIDGE_FORMAT))
            media = Media.objects.create(
                key='vid1', source=source,
                metadata=metadata_with(artists=['Some Artist']),
            )
            self.assertEqual(
                media.filepath.relative_to(source.directory_path).as_posix(),
                'Season 2017/s2017e091101 - Some Artist - no fancy stuff title [vid1].mkv',
            )
            nfo = ElementTree.fromstring(media.nfoxml)
            self.assertEqual(nfo.find('title').text, 'Some Artist - no fancy stuff title')
            self.assertEqual(nfo.find('season').text, '2017')
            self.assertEqual(nfo.find('episode').text, '91101')


class PlexNamingCommandTestCase(TestCase):

    def setUp(self):
        logging.disable(logging.CRITICAL)
        _clear_show_title_cache()

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def _live_music(self):
        source = make_source()
        source.make_directory()
        write_tvshow_nfo(source)
        (source.directory_path / 'season-poster.jpg').write_bytes(b'season-art')
        media = Media.objects.create(
            key='vid1', source=source,
            metadata=metadata_with(artists=['Hans Zimmer']),
        )
        old_path = download_dummy_file(media)
        return source, media, old_path

    def test_dry_run_prints_paths_and_changes_nothing(self):
        with temp_download_root():
            source, media, old_path = self._live_music()
            nfo_before = (source.directory_path / 'tvshow.nfo').read_bytes()
            output = run(
                '--source', source.key, '--artist-titles',
                '--show-title', 'Live Sessions',
            )
            self.assertIn('[DRY-RUN]', output)
            self.assertIn(str(old_path), output)
            self.assertIn(
                's2017e091101 - Hans Zimmer - no fancy stuff title [vid1].mkv', output,
            )
            self.assertIn("-> 'Live Sessions'", output)
            source.refresh_from_db()
            self.assertEqual(source.media_format, BRIDGE_FORMAT)
            self.assertTrue(old_path.exists())
            self.assertEqual(
                (source.directory_path / 'tvshow.nfo').read_bytes(), nfo_before,
            )

    def test_apply_renames_rewrites_nfos_and_keeps_the_show_title(self):
        with temp_download_root():
            source, media, old_path = self._live_music()
            with patch('sync.signals.save_all_media_for_source') as cascade:
                run(
                    '--source', source.key, '--artist-titles',
                    '--show-title', 'Live Sessions', '--apply',
                )
            cascade.assert_not_called()
            source.refresh_from_db()
            media.refresh_from_db()
            self.assertIn('{artist_title_bounded}', source.media_format)
            season = source.directory_path / 'Season 2017'
            new_video = season / 's2017e091101 - Hans Zimmer - no fancy stuff title [vid1].mkv'
            self.assertTrue(new_video.exists())
            self.assertFalse(old_path.exists())
            self.assertEqual(Path(media.media_file.path), new_video)
            self.assertEqual(new_video.read_bytes(), b'fake-mkv-bytes')
            nfo = ElementTree.fromstring(
                new_video.with_suffix('.nfo').read_text(encoding='utf-8'),
            )
            self.assertEqual(nfo.find('title').text, 'Hans Zimmer - no fancy stuff title')
            self.assertEqual(nfo.find('showtitle').text, 'Live Sessions')
            self.assertFalse(old_path.with_suffix('.nfo').exists())
            self.assertFalse((season / 'Season2017.jpg').exists())
            # the show folder (Kometa's match key) and its poster stay put
            self.assertEqual(source.directory, 'acq-src-PLabcdefghijklmnopqrstuv')
            self.assertEqual(
                (source.directory_path / 'season-poster.jpg').read_bytes(), b'season-art',
            )
            # tvshow.nfo is hand-owned now: TubeSync keeps it
            tvshow = source.directory_path / 'tvshow.nfo'
            root = ElementTree.fromstring(tvshow.read_bytes())
            self.assertEqual(root.findtext('title'), 'Live Sessions')
            self.assertEqual(root.findtext('sorttitle'), 'Live Sessions')
            self.assertEqual(
                [u.get('type') for u in root.iter('uniqueid')], ['youtube', 'tubesync'],
            )
            self.assertFalse(write_tvshow_nfo(source))
            self.assertEqual(ElementTree.fromstring(tvshow.read_bytes()).findtext('title'),
                             'Live Sessions')
            _clear_show_title_cache()
            self.assertEqual(resolve_show_title(source), 'Live Sessions')
            # a second run has nothing left to do
            output = run('--source', source.key, '--artist-titles', '--apply')
            self.assertIn('renamed/rewrote 1, busy 0, failed 0', output)
            self.assertTrue(new_video.exists())

    def test_artist_titles_alone_keep_the_automatic_show_title(self):
        with temp_download_root():
            source, media, old_path = self._live_music()
            tvshow = source.directory_path / 'tvshow.nfo'
            tvshow_before = tvshow.read_bytes()
            show_title = resolve_show_title(source)
            run('--source', source.key, '--artist-titles', '--apply')
            media.refresh_from_db()
            nfo = ElementTree.fromstring(
                Path(media.media_file.path).with_suffix('.nfo').read_text(encoding='utf-8'),
            )
            self.assertEqual(nfo.find('title').text, 'Hans Zimmer - no fancy stuff title')
            self.assertEqual(nfo.find('showtitle').text, show_title)
            # still TubeSync's own file, unchanged
            self.assertEqual(tvshow.read_bytes(), tvshow_before)
            self.assertIn(b'checksum="sha256:', tvshow_before)

    def test_show_title_alone_rewrites_nfos_without_renaming(self):
        with temp_download_root():
            source, media, old_path = self._live_music()
            run('--source', source.key, '--show-title', 'Live Sessions', '--apply')
            self.assertTrue(old_path.exists())
            nfo = ElementTree.fromstring(
                old_path.with_suffix('.nfo').read_text(encoding='utf-8'),
            )
            self.assertEqual(nfo.find('showtitle').text, 'Live Sessions')
            self.assertEqual(nfo.find('title').text, 'no fancy stuff title')

    def test_busy_media_fail_the_run_so_it_is_repeated(self):
        with temp_download_root():
            source, media, old_path = self._live_music()
            lock = MagicMock()
            lock.__enter__.side_effect = TaskLockedException('busy')
            with patch(
                'sync.management.commands.medianest_plex_naming.huey_lock_task',
                return_value=lock,
            ):
                with self.assertRaises(CommandError):
                    run('--source', source.key, '--artist-titles', '--apply')
            self.assertTrue(old_path.exists())

    def test_needs_an_action_and_one_matching_source(self):
        with temp_download_root():
            source = make_source()
            with self.assertRaises(CommandError):
                run('--artist-titles', '--source', 'nope')
            with self.assertRaises(CommandError):
                run('--source', source.key)

    def test_hand_owned_nfo_built_when_missing(self):
        with temp_download_root():
            source = make_source()
            source.make_directory()
            root = ElementTree.fromstring(hand_owned_tvshow_nfo(source, 'Live Sessions'))
            self.assertEqual(root.findtext('title'), 'Live Sessions')
            self.assertNotIn(
                'checksum', ''.join(str(u.attrib) for u in root.iter('uniqueid')),
            )
