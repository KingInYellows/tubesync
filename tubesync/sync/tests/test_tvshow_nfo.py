'''
    T2: automatic tvshow.nfo writer (sync/tvshow_nfo.py).

    Uses the same fixed Source configuration as test_media.py/
    test_filepath.py (1080p/VP9/OPUS) so the checked-in metadata fixtures
    format successfully. All filesystem writes go through a
    tempfile.TemporaryDirectory() with sync.models._migrations's shared
    media_file_storage.location patched to it -- never the real
    DOWNLOAD_ROOT/downloads.
'''
import logging
import tempfile
from contextlib import contextmanager
from unittest.mock import patch
from xml.etree import ElementTree

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from sync.choices import (
    Val, Fallback, SourceResolution,
    YouTube_AudioCodec, YouTube_VideoCodec,
    YouTube_SourceType,
)
from sync.models import Media, Metadata, Source
from sync.models._migrations import media_file_storage
from sync.tvshow_nfo import (
    build_tvshow_nfo, resolve_show_plot, resolve_show_studio,
    resolve_show_title, write_tvshow_nfo,
)

from .fixtures import all_test_metadata

# uploader='test uploader', playlist_title='test playlist'
metadata = all_test_metadata['boring']


@contextmanager
def temp_download_root():
    '''
        Points Source.directory_path (via media_file_storage.location) AND
        settings.DOWNLOAD_ROOT (which write_text_file's file_is_editable
        allow-list checks separately -- see sync/utils.py) at the same
        temporary directory, so a write_tvshow_nfo() call in a test lands
        entirely inside it and is never mistaken for a write outside the
        allowed paths. Never touches the real DOWNLOAD_ROOT/downloads.
    '''
    with tempfile.TemporaryDirectory() as tmp_dir:
        with (
            override_settings(DOWNLOAD_ROOT=tmp_dir),
            patch.object(media_file_storage, 'location', tmp_dir),
        ):
            yield tmp_dir


def make_source(**overrides):
    defaults = dict(
        source_type=Val(YouTube_SourceType.CHANNEL_ID),
        key='UCabcdefghijklmnopqrstuv',
        name='testname',
        directory='testdirectory',
        media_format=settings.MEDIA_FORMATSTR_DEFAULT,
        write_nfo=True,
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


class ResolveShowTitleTestCase(TestCase):

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.source = make_source()

    def test_falls_back_to_source_name_with_no_data(self):
        self.assertEqual(resolve_show_title(self.source), 'testname')
        self.assertIsNone(resolve_show_studio(self.source))
        self.assertEqual(resolve_show_plot(self.source), '')

    def test_uses_latest_medias_uploader_for_a_channel(self):
        Media.objects.create(key='m1', source=self.source, metadata=metadata)
        self.assertEqual(resolve_show_title(self.source), 'test uploader')
        self.assertEqual(resolve_show_studio(self.source), 'test uploader')

    def test_uses_latest_medias_playlist_title_for_a_playlist(self):
        playlist_source = make_source(
            source_type=Val(YouTube_SourceType.PLAYLIST),
            key='PLabcdefghijklmnopqrstuv',
            name='playlistname',
            directory='playlistdirectory',
        )
        Media.objects.create(key='p1', source=playlist_source, metadata=metadata)
        self.assertEqual(resolve_show_title(playlist_source), 'test playlist')

    def test_prefers_the_cached_channel_metadata_over_media(self):
        Media.objects.create(key='m1', source=self.source, metadata=metadata)
        Metadata.objects.create(
            site='Youtube', key=self.source.key,
            value={
                'title': 'Cached Channel Title',
                'description': 'Cached channel plot',
            },
        )
        self.assertEqual(resolve_show_title(self.source), 'Cached Channel Title')
        self.assertEqual(resolve_show_studio(self.source), 'Cached Channel Title')
        self.assertEqual(resolve_show_plot(self.source), 'Cached channel plot')

    def test_a_differently_keyed_cache_row_does_not_match(self):
        # A handle-based CHANNEL source's key never matches the cached
        # row's key (the resolved "UC..." id) -- falls through to media.
        Media.objects.create(key='m1', source=self.source, metadata=metadata)
        Metadata.objects.create(
            site='Youtube', key='UCsomeotherid',
            value={'title': 'Wrong Channel'},
        )
        self.assertEqual(resolve_show_title(self.source), 'test uploader')

    def test_unpublished_media_without_metadata_does_not_mask_the_uploader(self):
        # A freshly indexed row (published NULL, no metadata) must not win
        # the "latest media" lookup -- on PostgreSQL a plain DESC sort puts
        # NULLs first.
        Media.objects.create(
            key='m1', source=self.source, metadata=metadata,
            published=timezone.now(),
        )
        Media.objects.create(key='m2', source=self.source)
        self.assertEqual(resolve_show_title(self.source), 'test uploader')


class BuildTvshowNfoTestCase(TestCase):

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.source = make_source()

    def test_well_formed_with_special_characters_and_emoji(self):
        Metadata.objects.create(
            site='Youtube', key=self.source.key,
            value={
                'title': 'Foo & Bar <Baz> 🎉',
                'description': 'Plot with & and <tag> too 🎉',
            },
        )
        xml_str = build_tvshow_nfo(self.source)
        # Escaped in the serialized form, not raw "&"/"<" that would break
        # a naive f-string-built NFO (the bug F5 found in the upstream
        # create-tvshow-nfo command).
        self.assertIn('&amp;', xml_str)
        self.assertIn('&lt;Baz&gt;', xml_str)
        tree = ElementTree.fromstring(xml_str)  # raises if not well-formed
        title_text = tree.find('title').text
        self.assertIn('&', title_text)
        self.assertIn('<Baz>', title_text)
        self.assertNotIn('\U0001F389', title_text)
        plot_text = tree.find('plot').text
        self.assertIn('&', plot_text)
        self.assertIn('<tag>', plot_text)
        self.assertNotIn('\U0001F389', plot_text)

    def test_sorttitle_uniqueid_and_optional_elements(self):
        xml_str = build_tvshow_nfo(self.source)
        tree = ElementTree.fromstring(xml_str)
        self.assertEqual(tree.find('title').text, 'testname')
        self.assertEqual(tree.find('sorttitle').text, 'testname')
        uniqueid = tree.find('uniqueid')
        self.assertEqual(uniqueid.text, self.source.key)
        self.assertEqual(uniqueid.get('type'), 'youtube')
        self.assertEqual(uniqueid.get('default'), 'true')
        # No plot/studio known yet -- both are omitted rather than empty.
        self.assertIsNone(tree.find('plot'))
        self.assertIsNone(tree.find('studio'))

    def test_studio_present_once_known(self):
        Media.objects.create(key='m1', source=self.source, metadata=metadata)
        xml_str = build_tvshow_nfo(self.source)
        tree = ElementTree.fromstring(xml_str)
        self.assertEqual(tree.find('studio').text, 'test uploader')


class WriteTvshowNfoTestCase(TestCase):

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.source = make_source()

    def _nfo_path(self):
        return self.source.directory_path / 'tvshow.nfo'

    def test_writes_nothing_when_write_nfo_is_false(self):
        self.source.write_nfo = False
        with temp_download_root():
            self.source.make_directory()
            write_tvshow_nfo(self.source)
            self.assertFalse(self._nfo_path().exists())

    def test_writes_a_well_formed_file(self):
        with temp_download_root():
            self.source.make_directory()
            write_tvshow_nfo(self.source)
            nfo_path = self._nfo_path()
            self.assertTrue(nfo_path.exists())
            tree = ElementTree.fromstring(nfo_path.read_text(encoding='utf-8'))
            self.assertEqual(tree.find('title').text, 'testname')

    def test_idempotent_rewrite(self):
        with temp_download_root():
            self.source.make_directory()
            write_tvshow_nfo(self.source)
            nfo_path = self._nfo_path()
            first_content = nfo_path.read_text(encoding='utf-8')
            first_mtime = nfo_path.stat().st_mtime_ns

            # Second call, nothing changed: content and mtime untouched.
            write_tvshow_nfo(self.source)
            self.assertEqual(nfo_path.read_text(encoding='utf-8'), first_content)
            self.assertEqual(nfo_path.stat().st_mtime_ns, first_mtime)

            # Now a more informative title becomes available: rewritten.
            Media.objects.create(key='m1', source=self.source, metadata=metadata)
            write_tvshow_nfo(self.source)
            second_content = nfo_path.read_text(encoding='utf-8')
            self.assertNotEqual(second_content, first_content)
            self.assertIn('test uploader', second_content)

    def test_never_deletes_an_existing_file_it_does_not_own(self):
        with temp_download_root():
            self.source.make_directory()
            self.source.write_nfo = False
            write_tvshow_nfo(self.source)
            self.assertFalse(self._nfo_path().exists())
            # Simulate a pre-existing manually-placed file, then confirm
            # a disabled write_nfo still leaves it alone.
            self._nfo_path().write_text(
                '<tvshow><title>Manual</title></tvshow>', encoding='utf-8',
            )
            write_tvshow_nfo(self.source)
            self.assertIn('Manual', self._nfo_path().read_text(encoding='utf-8'))

    def test_missing_directory_is_skipped_without_raising(self):
        with temp_download_root():
            self.assertFalse(self.source.directory_path.exists())
            write_tvshow_nfo(self.source)
            self.assertFalse(self._nfo_path().exists())

    def test_non_utf8_existing_file_is_replaced_without_raising(self):
        with temp_download_root():
            self.source.make_directory()
            self._nfo_path().write_bytes(b'\xff\xfe not utf-8')
            write_tvshow_nfo(self.source)
            tree = ElementTree.fromstring(
                self._nfo_path().read_text(encoding='utf-8'),
            )
            self.assertEqual(tree.find('title').text, 'testname')

    def test_write_errors_are_logged_not_raised(self):
        with temp_download_root():
            self.source.make_directory()
            with (
                patch(
                    'sync.tvshow_nfo.write_text_file',
                    side_effect=OSError('disk full'),
                ),
                patch('sync.tvshow_nfo.log') as mock_log,
            ):
                write_tvshow_nfo(self.source)
            mock_log.exception.assert_called_once()
            self.assertFalse(self._nfo_path().exists())
