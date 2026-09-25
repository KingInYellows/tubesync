'''
    T2: automatic tvshow.nfo writer (sync/tvshow_nfo.py).

    Uses the same fixed Source configuration as test_media.py/
    test_filepath.py (1080p/VP9/OPUS) so the checked-in metadata fixtures
    format successfully. All filesystem writes go through a
    tempfile.TemporaryDirectory() with sync.models._migrations's shared
    media_file_storage.location patched to it -- never the real
    DOWNLOAD_ROOT/downloads.
'''
import json
import logging
import re
import tempfile
import time
from contextlib import contextmanager
from collections import deque
from unittest.mock import PropertyMock, patch
from xml.etree import ElementTree

from django.conf import settings
from django.db import DatabaseError, InterfaceError, connection, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from sync.choices import (
    Val, Fallback, SourceResolution,
    YouTube_AudioCodec, YouTube_VideoCodec,
    YouTube_SourceType,
)
from sync.models import Media, Metadata, Source
from sync.models._migrations import media_file_storage
from sync import tvshow_nfo as tvshow_nfo_module
from sync.tvshow_nfo import (
    _clear_show_title_cache, _invalidate_show_title_cache,
    _show_title_cache, _store_show_title,
    build_tvshow_nfo, resolve_show_title, tvshow_nfo_needs_write,
    write_tvshow_nfo,
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
        _clear_show_title_cache()
        self.source = make_source()

    def tearDown(self):
        _clear_show_title_cache()

    def test_falls_back_to_source_name_with_no_data(self):
        self.assertEqual(resolve_show_title(self.source), 'testname')
        tree = ElementTree.fromstring(build_tvshow_nfo(self.source))
        self.assertIsNone(tree.find('studio'))
        self.assertIsNone(tree.find('plot'))

    def test_uses_latest_medias_uploader_for_a_channel(self):
        Media.objects.create(key='m1', source=self.source, metadata=metadata)
        self.assertEqual(resolve_show_title(self.source), 'test uploader')
        tree = ElementTree.fromstring(build_tvshow_nfo(self.source))
        self.assertEqual(tree.find('studio').text, 'test uploader')

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
        tree = ElementTree.fromstring(build_tvshow_nfo(self.source))
        self.assertEqual(tree.find('studio').text, 'Cached Channel Title')
        self.assertEqual(tree.find('plot').text, 'Cached channel plot')

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


    def test_metadata_only_in_the_related_table_is_used(self):
        media = Media.objects.create(key='m1', source=self.source)
        media.ingest_metadata(json.loads(metadata))
        self.assertIsNone(Media.objects.get(pk=media.pk).metadata)
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

    def test_tubesync_ownership_marker_uses_the_immutable_uuid(self):
        # A second, non-default uniqueid tied to the source's immutable
        # pk -- ignored by Kodi/Plex/Jellyfin, but lets this writer keep
        # recognising the file after a source-key edit.
        xml_str = build_tvshow_nfo(self.source)
        tree = ElementTree.fromstring(xml_str)
        ids = tree.findall('uniqueid')
        self.assertEqual(len(ids), 2)
        tubesync_id = ids[1]
        self.assertEqual(tubesync_id.get('type'), 'tubesync')
        self.assertIsNone(tubesync_id.get('default'))
        self.assertEqual(tubesync_id.text, str(self.source.uuid))

    def test_studio_present_once_known(self):
        Media.objects.create(key='m1', source=self.source, metadata=metadata)
        xml_str = build_tvshow_nfo(self.source)
        tree = ElementTree.fromstring(xml_str)
        self.assertEqual(tree.find('studio').text, 'test uploader')


class WriteTvshowNfoTestCase(TestCase):

    def setUp(self):
        logging.disable(logging.CRITICAL)
        _clear_show_title_cache()
        self.source = make_source()

    def tearDown(self):
        _clear_show_title_cache()

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

    def test_an_episode_nfo_at_the_same_path_is_left_alone(self):
        episode = '<episodedetails><title>Video</title></episodedetails>'
        with temp_download_root():
            self.source.make_directory()
            self._nfo_path().write_text(episode, encoding='utf-8')
            with patch('sync.tvshow_nfo.log') as mock_log:
                write_tvshow_nfo(self.source)
            mock_log.warning.assert_called_once()
            self.assertEqual(
                self._nfo_path().read_text(encoding='utf-8'), episode,
            )

    def test_a_tvshow_nfo_it_did_not_write_is_left_alone(self):
        manual = '<tvshow><title>Hand written</title></tvshow>'
        with temp_download_root():
            self.source.make_directory()
            self._nfo_path().write_text(manual, encoding='utf-8')
            write_tvshow_nfo(self.source)
            self.assertEqual(self._nfo_path().read_text(encoding='utf-8'), manual)

    def test_builds_the_nfo_only_once(self):
        '''
            write_tvshow_nfo() used to call tvshow_nfo_needs_write() (which
            builds the NFO to compare bytes) and then build it AGAIN
            itself -- doubling the underlying metadata queries on every
            index_source/download_source_images/download_media_metadata
            run. Both public functions now share one call through
            _tvshow_nfo_content_to_write().
        '''
        with temp_download_root():
            self.source.make_directory()
            with patch.object(
                tvshow_nfo_module, 'build_tvshow_nfo',
                wraps=tvshow_nfo_module.build_tvshow_nfo,
            ) as mock_build:
                write_tvshow_nfo(self.source)
            mock_build.assert_called_once()

            # Nothing changed: tvshow_nfo_needs_write() alone also builds
            # exactly once (to compare against the bytes on disk).
            with patch.object(
                tvshow_nfo_module, 'build_tvshow_nfo',
                wraps=tvshow_nfo_module.build_tvshow_nfo,
            ) as mock_build_needs:
                self.assertFalse(tvshow_nfo_needs_write(self.source))
            mock_build_needs.assert_called_once()

    def test_still_owned_and_rewritten_after_the_source_key_changes(self):
        # A source-update form edit to `key` must not orphan a file this
        # writer already created for it -- the `tubesync`/uuid marker
        # keeps it recognised as owned so it gets refreshed instead of
        # being frozen with a stale youtube uniqueid/title forever.
        with temp_download_root():
            self.source.make_directory()
            write_tvshow_nfo(self.source)
            nfo_path = self._nfo_path()
            first_content = nfo_path.read_text(encoding='utf-8')

            self.source.key = 'UCnewkeyabcdefghijklmnop'
            self.source.name = 'renamedname'
            self.source.save()
            with patch('sync.tvshow_nfo.log') as mock_log:
                write_tvshow_nfo(self.source)
            mock_log.warning.assert_not_called()

            second_content = nfo_path.read_text(encoding='utf-8')
            self.assertNotEqual(second_content, first_content)
            tree = ElementTree.fromstring(second_content)
            self.assertEqual(tree.find('title').text, 'renamedname')
            ids = {u.get('type'): u.text for u in tree.findall('uniqueid')}
            self.assertEqual(ids['youtube'], 'UCnewkeyabcdefghijklmnop')
            self.assertEqual(ids['tubesync'], str(self.source.uuid))

    def test_a_foreign_tvshow_nfo_is_still_left_alone_after_a_key_change(self):
        # A hand-written (or other-writer) file with neither marker must
        # stay foreign even once the source's key happens to change.
        manual = '<tvshow><title>Hand written</title></tvshow>'
        with temp_download_root():
            self.source.make_directory()
            self._nfo_path().write_text(manual, encoding='utf-8')
            self.source.key = 'UCnewkeyabcdefghijklmnop'
            self.source.save()
            write_tvshow_nfo(self.source)
            self.assertEqual(self._nfo_path().read_text(encoding='utf-8'), manual)

    def test_missing_directory_is_skipped_without_raising(self):
        with temp_download_root():
            self.assertFalse(self.source.directory_path.exists())
            write_tvshow_nfo(self.source)
            self.assertFalse(self._nfo_path().exists())

    def test_non_utf8_existing_file_is_left_alone_without_raising(self):
        # An unparseable but non-empty file is now treated as foreign (a
        # Kodi URL-only/combination NFO or upstream create-tvshow-nfo's
        # unescaped "&" output can both look like this) -- it must not be
        # silently replaced just because ElementTree cannot parse it.
        with temp_download_root():
            self.source.make_directory()
            raw = b'\xff\xfe not utf-8'
            self._nfo_path().write_bytes(raw)
            with patch('sync.tvshow_nfo.log') as mock_log:
                write_tvshow_nfo(self.source)
            mock_log.warning.assert_called_once()
            self.assertEqual(self._nfo_path().read_bytes(), raw)

    def test_url_only_nfo_is_left_alone(self):
        # A Kodi "URL-only" NFO: a single line with no XML markup at all.
        # Not well-formed XML, so ElementTree.fromstring raises ParseError
        # -- must still be treated as foreign, not overwritten.
        url_only = 'https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv\n'
        with temp_download_root():
            self.source.make_directory()
            self._nfo_path().write_text(url_only, encoding='utf-8')
            with patch('sync.tvshow_nfo.log') as mock_log:
                write_tvshow_nfo(self.source)
            mock_log.warning.assert_called_once()
            self.assertEqual(
                self._nfo_path().read_text(encoding='utf-8'), url_only,
            )

    def test_zero_byte_existing_file_is_still_replaced(self):
        # Unlike a non-empty unparseable file, a zero-byte file carries no
        # content to protect and remains replaceable, same as an absent one.
        with temp_download_root():
            self.source.make_directory()
            self._nfo_path().write_bytes(b'')
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


class ResolveShowTitleCacheTestCase(TestCase):
    '''
        resolve_show_title()'s process-local TTL cache: hit/expiry/
        invalidation/bypass/error-fallback behaviour. `build_tvshow_nfo()`
        is untouched by this cache -- only `resolve_show_title()` (and,
        through it, `Media.nfoxml`) is covered here.
    '''

    def setUp(self):
        logging.disable(logging.CRITICAL)
        _clear_show_title_cache()
        self.source = make_source()
        Metadata.objects.create(
            site='Youtube', key=self.source.key,
            value={'title': 'Cached Channel Title'},
        )

    def tearDown(self):
        _clear_show_title_cache()

    def test_second_call_within_the_ttl_is_served_from_cache(self):
        self.assertEqual(resolve_show_title(self.source), 'Cached Channel Title')
        # Populating the cache above already queried the DB once; a second
        # call within the TTL must not query at all.
        with self.assertNumQueries(0):
            self.assertEqual(
                resolve_show_title(self.source), 'Cached Channel Title',
            )

    def test_cache_expires_after_the_ttl(self):
        self.assertEqual(resolve_show_title(self.source), 'Cached Channel Title')
        # Change the underlying data, then simulate the clock moving past
        # the TTL -- the next call must re-query rather than keep serving
        # the value cached above.
        Metadata.objects.filter(key=self.source.key).update(
            value={'title': 'Updated Channel Title'},
        )
        with patch(
            'sync.tvshow_nfo.time.monotonic',
            return_value=time.monotonic() + 61,
        ):
            self.assertEqual(
                resolve_show_title(self.source), 'Updated Channel Title',
            )

    def test_write_tvshow_nfo_invalidates_the_cache(self):
        self.assertEqual(resolve_show_title(self.source), 'Cached Channel Title')
        Metadata.objects.filter(key=self.source.key).update(
            value={'title': 'Refreshed Channel Title'},
        )
        with temp_download_root():
            self.source.make_directory()
            # Still within the TTL: without invalidation this would keep
            # returning the stale cached value.
            write_tvshow_nfo(self.source)
        self.assertEqual(
            resolve_show_title(self.source), 'Refreshed Channel Title',
        )

    def test_unsaved_source_bypasses_the_cache(self):
        unsaved = Source(
            source_type=Val(YouTube_SourceType.CHANNEL_ID),
            key='UCunsavedabcdefghijklmno',
            name='unsavedname',
            directory='unsaveddirectory',
            media_format=settings.MEDIA_FORMATSTR_DEFAULT,
            source_resolution=Val(SourceResolution.VIDEO_1080P),
            source_vcodec=Val(YouTube_VideoCodec.VP9),
            source_acodec=Val(YouTube_AudioCodec.OPUS),
        )
        # Source.uuid (its pk) has a client-side `default=uuid.uuid4`, so a
        # freshly constructed instance already carries a pk before it is
        # ever saved. Force the actual "never persisted, no stable key"
        # state resolve_show_title()'s cache bypass guards against.
        unsaved.pk = None
        self.assertIsNone(unsaved.pk)
        with (
            patch(
                'sync.tvshow_nfo._cached_channel_metadata', return_value=None,
            ),
            patch(
                'sync.tvshow_nfo._resolve_show_title_from_data',
                return_value='Live Title',
            ) as mock_resolve,
        ):
            self.assertEqual(resolve_show_title(unsaved), 'Live Title')
            # A second call must resolve again, not serve a cached value --
            # an unsaved source has no stable key to cache under.
            self.assertEqual(resolve_show_title(unsaved), 'Live Title')
        self.assertEqual(mock_resolve.call_count, 2)
        self.assertEqual(_show_title_cache, {})

    def test_database_error_falls_back_to_source_name(self):
        with (
            patch(
                'sync.tvshow_nfo._cached_channel_metadata',
                side_effect=DatabaseError('boom'),
            ),
            patch('sync.tvshow_nfo.log') as mock_log,
        ):
            self.assertEqual(resolve_show_title(self.source), self.source.name)
        mock_log.exception.assert_called_once()
        # A fallback produced by an error must not be cached -- otherwise a
        # transient DB error would pin `source.name` for the whole TTL.
        self.assertEqual(_show_title_cache, {})

    def test_nfoxml_still_renders_when_show_title_resolution_fails(self):
        media = Media.objects.create(
            key='m1', source=self.source, metadata=metadata,
        )
        with patch(
            'sync.tvshow_nfo._cached_channel_metadata',
            side_effect=DatabaseError('boom'),
        ):
            xml_str = media.nfoxml
        tree = ElementTree.fromstring(xml_str)
        self.assertEqual(tree.find('showtitle').text, self.source.name)


def media_metadata(**fields):
    '''The "boring" fixture's JSON with `fields` replaced (None removes).'''
    data = json.loads(metadata)
    for key, value in fields.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return json.dumps(data)


class ShowTitleTiersTestCase(TestCase):

    def setUp(self):
        logging.disable(logging.CRITICAL)
        _clear_show_title_cache()
        self.source = make_source()

    def tearDown(self):
        _clear_show_title_cache()

    def test_channel_is_preferred_over_uploader(self):
        Media.objects.create(
            key='m1', source=self.source,
            metadata=media_metadata(channel='The Channel', uploader='The Uploader'),
        )
        self.assertEqual(resolve_show_title(self.source), 'The Channel')

    def test_a_stub_row_without_a_name_does_not_hide_an_older_one(self):
        Media.objects.create(
            key='older', source=self.source, metadata=metadata,
            published=timezone.now() - timezone.timedelta(days=1),
        )
        Media.objects.create(
            key='stub', source=self.source,
            metadata=media_metadata(channel=None, uploader=None),
            published=timezone.now(),
        )
        self.assertEqual(resolve_show_title(self.source), 'test uploader')

    def test_same_published_ties_break_by_newest_created(self):
        published = timezone.now()
        Media.objects.create(
            key='first', source=self.source, published=published,
            metadata=media_metadata(uploader='First Name'),
        )
        Media.objects.create(
            key='second', source=self.source, published=published,
            metadata=media_metadata(uploader='Second Name'),
        )
        self.assertEqual(resolve_show_title(self.source), 'Second Name')

    def test_cached_channel_row_prefers_channel_over_its_tab_title(self):
        Metadata.objects.create(
            site='YoutubeTab', key=self.source.key,
            value={'title': 'The Channel - Videos', 'channel': 'The Channel'},
        )
        self.assertEqual(resolve_show_title(self.source), 'The Channel')

    def test_media_retrieved_after_the_cached_row_wins_for_a_channel(self):
        now = timezone.now()
        Metadata.objects.create(
            site='YoutubeTab', key=self.source.key,
            value={'channel': 'Old Name'},
            retrieved=now - timezone.timedelta(days=2),
        )
        media = Media.objects.create(key='renamed', source=self.source)
        media.ingest_metadata(json.loads(media_metadata(
            channel='New Name', epoch=int(now.timestamp()),
        )))
        self.assertEqual(resolve_show_title(self.source), 'New Name')
        Metadata.objects.filter(media__isnull=True).update(
            retrieved=now + timezone.timedelta(days=1),
        )
        _clear_show_title_cache()
        self.assertEqual(resolve_show_title(self.source), 'Old Name')

    def test_playlist_studio_is_the_cached_playlist_title(self):
        playlist_source = make_source(
            source_type=Val(YouTube_SourceType.PLAYLIST),
            key='PLabcdefghijklmnopqrstuv',
            name='playlistname',
            directory='playlistdirectory',
        )
        Metadata.objects.create(
            site='YoutubeTab', key=playlist_source.key,
            value={'title': 'The Playlist', 'channel': 'Its Owner'},
        )
        tree = ElementTree.fromstring(build_tvshow_nfo(playlist_source))
        self.assertEqual(tree.find('title').text, 'The Playlist')
        self.assertEqual(tree.find('studio').text, 'The Playlist')

    def test_an_emoji_only_name_falls_through_to_the_next_tier(self):
        Media.objects.create(
            key='emoji', source=self.source,
            metadata=media_metadata(channel='\U0001F389', uploader='\U0001F389'),
        )
        self.assertEqual(resolve_show_title(self.source), 'testname')
        emoji_source = make_source(
            key='UCemojiabcdefghijklmnopq', name='\U0001F389',
            directory='emojidirectory',
        )
        self.assertEqual(resolve_show_title(emoji_source), '\U0001F389')
        tree = ElementTree.fromstring(build_tvshow_nfo(emoji_source))
        self.assertEqual(tree.find('title').text, '\U0001F389')

    def test_title_and_showtitle_are_stripped_alike(self):
        self.source.name = '  spaced name  '
        self.source.save()
        tree = ElementTree.fromstring(build_tvshow_nfo(self.source))
        self.assertEqual(tree.find('title').text, 'spaced name')
        self.assertEqual(resolve_show_title(self.source), 'spaced name')


class TvshowNfoOwnershipTestCase(TestCase):

    def setUp(self):
        logging.disable(logging.CRITICAL)
        _clear_show_title_cache()
        self.source = make_source()

    def tearDown(self):
        _clear_show_title_cache()

    def _nfo_path(self):
        return self.source.directory_path / 'tvshow.nfo'

    def test_the_checksum_marker_verifies(self):
        xml_str = build_tvshow_nfo(self.source)
        tree = ElementTree.fromstring(xml_str)
        tubesync_id = [
            u for u in tree.findall('uniqueid') if u.get('type') == 'tubesync'
        ][0]
        self.assertRegex(tubesync_id.get('checksum'), r'^sha256:[0-9a-f]{64}$')
        with temp_download_root():
            self.source.make_directory()
            write_tvshow_nfo(self.source)
            self.assertEqual(self._nfo_path().read_text(encoding='utf-8'), xml_str)
            Media.objects.create(key='m1', source=self.source, metadata=metadata)
            write_tvshow_nfo(self.source)
            self.assertIn(
                'test uploader', self._nfo_path().read_text(encoding='utf-8'),
            )

    def test_a_hand_edited_file_is_kept_and_names_the_episodes(self):
        with temp_download_root():
            self.source.make_directory()
            write_tvshow_nfo(self.source)
            edited = self._nfo_path().read_text(encoding='utf-8').replace(
                '<title>testname</title>', '<title>My Show</title>',
            )
            self._nfo_path().write_text(edited, encoding='utf-8')
            media = Media.objects.create(
                key='m1', source=self.source, metadata=metadata,
            )
            with patch('sync.tvshow_nfo.log') as mock_log:
                write_tvshow_nfo(self.source)
            mock_log.warning.assert_called_once()
            self.assertIn('edited', mock_log.warning.call_args.args[0])
            self.assertEqual(self._nfo_path().read_text(encoding='utf-8'), edited)
            self.assertEqual(resolve_show_title(self.source), 'My Show')
            tree = ElementTree.fromstring(media.nfoxml)
            self.assertEqual(tree.find('showtitle').text, 'My Show')

    def test_a_youtube_uniqueid_alone_does_not_make_a_file_ours(self):
        manual = (
            '<tvshow><title>Kept</title><genre>Custom</genre>'
            f'<uniqueid type="youtube">{self.source.key}</uniqueid></tvshow>'
        )
        with temp_download_root():
            self.source.make_directory()
            self._nfo_path().write_text(manual, encoding='utf-8')
            write_tvshow_nfo(self.source)
            self.assertEqual(self._nfo_path().read_text(encoding='utf-8'), manual)

    def test_a_file_without_the_checksum_is_kept(self):
        # No release ever wrote the id without a checksum, so a file like
        # this can only be an edited copy.
        edited = (
            '<tvshow><title>Old</title>'
            f'<uniqueid type="tubesync">{self.source.uuid}</uniqueid></tvshow>'
        )
        with temp_download_root():
            self.source.make_directory()
            self._nfo_path().write_text(edited, encoding='utf-8')
            write_tvshow_nfo(self.source)
            self.assertEqual(self._nfo_path().read_text(encoding='utf-8'), edited)
            self.assertEqual(resolve_show_title(self.source), 'Old')

    def test_a_reformatted_checksum_marks_the_file_edited(self):
        with temp_download_root():
            self.source.make_directory()
            write_tvshow_nfo(self.source)
            original = self._nfo_path().read_text(encoding='utf-8')
            # An editor that reorders the attributes, and one that drops
            # the checksum: the content is otherwise untouched.
            reordered = original.replace(
                '<uniqueid type="tubesync" checksum=',
                '<uniqueid checksum=',
            ).replace(
                f'>{self.source.uuid}</uniqueid>',
                f' type="tubesync">{self.source.uuid}</uniqueid>',
            )
            dropped = re.sub(r' checksum="[^"]*"', '', original)
            for variant in (reordered, dropped):
                with self.subTest(variant=variant):
                    self._nfo_path().write_text(variant, encoding='utf-8')
                    Media.objects.get_or_create(
                        key='m1', source=self.source, defaults={'metadata': metadata},
                    )
                    with patch('sync.tvshow_nfo.log') as mock_log:
                        write_tvshow_nfo(self.source)
                    mock_log.warning.assert_called_once()
                    self.assertEqual(
                        self._nfo_path().read_text(encoding='utf-8'), variant,
                    )

    def test_the_file_is_read_once_per_title_lookup(self):
        upstream = '<tvshow><title>Upstream Title</title></tvshow>'
        with temp_download_root():
            self.source.make_directory()
            self._nfo_path().write_text(upstream, encoding='utf-8')
            real_read_bytes = type(self._nfo_path()).read_bytes
            with patch.object(
                type(self._nfo_path()), 'read_bytes', autospec=True,
                side_effect=real_read_bytes,
            ) as read_bytes:
                self.assertEqual(resolve_show_title(self.source), 'Upstream Title')
            self.assertEqual(read_bytes.call_count, 1)

    def test_episodes_follow_a_create_tvshow_nfo_title(self):
        # What the upstream create-tvshow-nfo command writes: source.name
        # and an upper-case "Youtube" id this writer does not own.
        upstream = (
            '<tvshow><title>Upstream Title</title>'
            f'<uniqueid type="Youtube" default="true">{self.source.key}'
            '</uniqueid></tvshow>'
        )
        with temp_download_root():
            self.source.make_directory()
            self._nfo_path().write_text(upstream, encoding='utf-8')
            media = Media.objects.create(
                key='m1', source=self.source, metadata=metadata,
            )
            self.assertEqual(resolve_show_title(self.source), 'Upstream Title')
            tree = ElementTree.fromstring(media.nfoxml)
            self.assertEqual(tree.find('showtitle').text, 'Upstream Title')


class ShowTitleCacheSafetyTestCase(TestCase):

    def setUp(self):
        logging.disable(logging.CRITICAL)
        _clear_show_title_cache()
        self.source = make_source()

    def tearDown(self):
        _clear_show_title_cache()

    def test_a_lookup_invalidated_while_running_is_not_stored(self):
        def invalidate_mid_lookup(source):
            _invalidate_show_title_cache(source)
            return None

        with patch(
            'sync.tvshow_nfo._cached_channel_metadata',
            side_effect=invalidate_mid_lookup,
        ):
            self.assertEqual(resolve_show_title(self.source), 'testname')
        self.assertEqual(_show_title_cache, {})
        resolve_show_title(self.source)
        self.assertIn(self.source.pk, _show_title_cache)

    def test_expired_entries_are_pruned(self):
        other = make_source(
            key='UCotherabcdefghijklmnopq', name='other', directory='other',
        )
        resolve_show_title(other)
        self.assertIn(other.pk, _show_title_cache)
        with patch(
            'sync.tvshow_nfo.time.monotonic',
            return_value=time.monotonic() + 61,
        ):
            _store_show_title(self.source, 0, 'fresh')
        self.assertNotIn(other.pk, _show_title_cache)
        self.assertIn(self.source.pk, _show_title_cache)

    def test_interface_error_falls_back_to_source_name(self):
        with (
            patch(
                'sync.tvshow_nfo._cached_channel_metadata',
                side_effect=InterfaceError('connection already closed'),
            ),
            patch('sync.tvshow_nfo.log') as mock_log,
        ):
            self.assertEqual(resolve_show_title(self.source), 'testname')
        mock_log.exception.assert_called_once()

    def test_the_lookup_runs_in_a_savepoint(self):
        with transaction.atomic():
            outer = len(connection.savepoint_ids)

            def fail_inside_a_savepoint(source):
                self.assertGreater(len(connection.savepoint_ids), outer)
                raise DatabaseError('boom')

            with patch(
                'sync.tvshow_nfo._cached_channel_metadata',
                side_effect=fail_inside_a_savepoint,
            ):
                self.assertEqual(resolve_show_title(self.source), 'testname')
            self.assertFalse(connection.needs_rollback)
            self.assertEqual(Source.objects.filter(pk=self.source.pk).count(), 1)

    def test_unexpected_errors_are_logged_not_raised(self):
        with (
            temp_download_root(),
            patch(
                'sync.tvshow_nfo.build_tvshow_nfo',
                side_effect=AttributeError('bug'),
            ),
            patch('sync.tvshow_nfo.log') as mock_log,
        ):
            self.source.make_directory()
            write_tvshow_nfo(self.source)
        mock_log.exception.assert_called_once()

    def test_a_non_dict_cached_value_is_ignored(self):
        Metadata.objects.create(
            site='YoutubeTab', key=self.source.key, value=['not', 'a', 'dict'],
        )
        self.assertEqual(resolve_show_title(self.source), 'testname')
        tree = ElementTree.fromstring(build_tvshow_nfo(self.source))
        self.assertEqual(tree.find('title').text, 'testname')
        self.assertIsNone(tree.find('plot'))


class TasksWriteTvshowNfoTestCase(TestCase):
    '''The three task call sites refresh tvshow.nfo for their source.'''

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.source = make_source(download_media=True)

    def assert_called_for_source(self, mock_write):
        mock_write.assert_called_once()
        self.assertEqual(mock_write.call_args.args[0].pk, self.source.pk)

    def test_index_source(self):
        from sync.tasks import index_source
        fake_video = {
            'id': 'newvid1',
            'duration': 120,
            'title': 'New Video',
            'ie_key': 'Youtube',
            'timestamp': int(timezone.now().timestamp()),
        }
        with (
            patch.object(Source, 'index_media', return_value=deque([fake_video])),
            patch('sync.tasks.write_tvshow_nfo') as mock_write,
        ):
            index_source.call_local(str(self.source.pk))
        self.assert_called_for_source(mock_write)

    def test_download_source_images(self):
        from sync.tasks import download_source_images
        with (
            patch.object(
                Source, 'get_image_url', new_callable=PropertyMock,
                return_value=(None, None, None),
            ),
            patch('sync.tasks.write_tvshow_nfo') as mock_write,
        ):
            download_source_images.call_local(str(self.source.pk))
        self.assert_called_for_source(mock_write)

    def test_download_source_images_refreshes_the_nfo_when_an_image_fails(self):
        from sync.tasks import download_source_images
        with (
            patch.object(
                Source, 'get_image_url', new_callable=PropertyMock,
                return_value=('https://example.invalid/a.jpg', None, None),
            ),
            patch('sync.tasks.get_remote_image', side_effect=OSError('boom')),
            patch('sync.tasks.write_tvshow_nfo') as mock_write,
        ):
            with self.assertRaises(OSError):
                download_source_images.call_local(str(self.source.pk))
        self.assert_called_for_source(mock_write)

    def test_download_media_metadata(self):
        from sync.tasks import download_media_metadata
        media = Media.objects.create(
            key='m1', source=self.source, published=timezone.now(),
        )
        response = json.loads(all_test_metadata['minimal'])
        with (
            patch.object(Media, 'index_metadata', return_value=response),
            patch('sync.tasks.write_tvshow_nfo') as mock_write,
        ):
            download_media_metadata.call_local(str(media.pk))
        self.assert_called_for_source(mock_write)


class TvshowNfoFollowUp5TestCase(TestCase):
    '''Emoji-only show titles and symlinked tvshow.nfo paths.'''

    def setUp(self):
        logging.disable(logging.CRITICAL)
        _clear_show_title_cache()
        self.source = make_source()

    def tearDown(self):
        _clear_show_title_cache()

    def _nfo_path(self):
        return self.source.directory_path / 'tvshow.nfo'

    def test_an_emoji_only_show_title_is_kept_in_both_nfos(self):
        self.source.name = '🎉🎉'
        self.source.save()
        tree = ElementTree.fromstring(build_tvshow_nfo(self.source))
        self.assertEqual(tree.find('title').text, '🎉🎉')
        media = Media.objects.create(key='m1', source=self.source, metadata=metadata)
        with patch('sync.tvshow_nfo.resolve_show_title', return_value='🎉🎉'):
            tree = ElementTree.fromstring(media.nfoxml)
        self.assertEqual(tree.find('showtitle').text, '🎉🎉')

    def test_a_dangling_symlink_is_left_alone(self):
        with temp_download_root():
            self.source.make_directory()
            self._nfo_path().symlink_to(self.source.directory_path / 'missing.nfo')
            with patch('sync.tvshow_nfo.log') as mock_log:
                write_tvshow_nfo(self.source)
            self.assertTrue(self._nfo_path().is_symlink())
            self.assertFalse(self._nfo_path().exists())
            self.assertIn('symlink', mock_log.warning.call_args.args[0])
            self.assertEqual(resolve_show_title(self.source), 'testname')

    def test_a_live_symlink_to_an_owned_file_is_left_alone(self):
        with temp_download_root():
            self.source.make_directory()
            write_tvshow_nfo(self.source)
            target = self.source.directory_path / 'elsewhere.nfo'
            self._nfo_path().rename(target)
            self._nfo_path().symlink_to(target)
            before = target.read_text(encoding='utf-8')
            # New data would change the file's content.
            Media.objects.create(key='m1', source=self.source, metadata=metadata)
            _clear_show_title_cache()
            with patch('sync.tvshow_nfo.log') as mock_log:
                write_tvshow_nfo(self.source)
            self.assertTrue(self._nfo_path().is_symlink())
            self.assertEqual(target.read_text(encoding='utf-8'), before)
            self.assertIn('symlink', mock_log.warning.call_args.args[0])
            # Its <title> still names the show.
            self.assertEqual(resolve_show_title(self.source), 'testname')


class TvshowNfoFollowUp6TestCase(TestCase):
    '''Foreign tvshow.nfo files with odd encodings or emoji titles.'''

    def setUp(self):
        logging.disable(logging.CRITICAL)
        _clear_show_title_cache()
        self.source = make_source()

    def tearDown(self):
        _clear_show_title_cache()

    def _nfo_path(self):
        return self.source.directory_path / 'tvshow.nfo'

    def test_an_unreadable_xml_encoding_is_left_alone(self):
        # "ANSI" makes ElementTree raise LookupError and "UTF-32" a
        # ValueError, not ParseError. Both must count as foreign files.
        for encoding in ('ANSI', 'UTF-32'):
            with self.subTest(encoding=encoding), temp_download_root():
                _clear_show_title_cache()
                self.source.make_directory()
                raw = (
                    f'<?xml version="1.0" encoding="{encoding}"?>'
                    '<tvshow><title>Hand</title></tvshow>'
                ).encode('ascii')
                self._nfo_path().write_bytes(raw)
                media = Media.objects.create(
                    key=f'm-{encoding}', source=self.source, metadata=metadata,
                )
                with patch('sync.tvshow_nfo.log') as mock_log:
                    write_tvshow_nfo(self.source)
                self.assertIn(
                    'could not be parsed', mock_log.warning.call_args.args[0],
                )
                self.assertEqual(self._nfo_path().read_bytes(), raw)
                # Falls back to TubeSync's own title (the media's uploader).
                self.assertEqual(resolve_show_title(self.source), 'test uploader')
                tree = ElementTree.fromstring(media.nfoxml)
                self.assertEqual(tree.find('showtitle').text, 'test uploader')

    def test_a_foreign_title_keeps_its_emoji(self):
        for title in ('🎮 Gaming', '🎉'):
            with self.subTest(title=title), temp_download_root():
                _clear_show_title_cache()
                self.source.make_directory()
                self._nfo_path().write_text(
                    f'<tvshow><title> {title} </title></tvshow>',
                    encoding='utf-8',
                )
                media = Media.objects.create(
                    key=f'm-{len(title)}', source=self.source, metadata=metadata,
                )
                self.assertEqual(resolve_show_title(self.source), title)
                tree = ElementTree.fromstring(media.nfoxml)
                self.assertEqual(tree.find('showtitle').text, title)

    def test_own_titles_still_drop_emoji(self):
        self.source.name = '🎮 Gaming'
        self.source.save()
        media = Media.objects.create(key='m1', source=self.source, metadata=metadata)
        with patch('sync.tvshow_nfo._resolve_show_title_from_data', return_value=None):
            _clear_show_title_cache()
            self.assertEqual(resolve_show_title(self.source), 'Gaming')
            tree = ElementTree.fromstring(media.nfoxml)
        self.assertEqual(tree.find('showtitle').text, 'Gaming')
