import json
import logging
from datetime import timedelta
from unittest.mock import patch
from PIL import Image
from django.test import TestCase, override_settings
from django.utils import timezone
from sync.models import Source, Media
from sync.tasks import (
    cleanup_old_media,
    download_media_image,
    download_media_metadata,
)
from .fixtures import all_test_metadata

class TasksTestCase(TestCase):

    def setUp(self):
        # Disable general logging for test case
        logging.disable(logging.CRITICAL)

    def test_delete_old_media(self):
        src1 = Source.objects.create(key='aaa', name='aaa', directory='/tmp/a', delete_old_media=False, days_to_keep=14)
        src2 = Source.objects.create(key='bbb', name='bbb', directory='/tmp/b', delete_old_media=True, days_to_keep=14)

        now = timezone.now()

        m11 = Media.objects.create(source=src1, downloaded=True, key='a11', download_date=now - timedelta(days=5)) # noqa: F841
        m12 = Media.objects.create(source=src1, downloaded=True, key='a12', download_date=now - timedelta(days=25)) # noqa: F841
        m13 = Media.objects.create(source=src1, downloaded=False, key='a13') # noqa: F841

        m21 = Media.objects.create(source=src2, downloaded=True, key='a21', download_date=now - timedelta(days=5)) # noqa: F841
        m22 = Media.objects.create(source=src2, downloaded=True, key='a22', download_date=now - timedelta(days=25))
        m23 = Media.objects.create(source=src2, downloaded=False, key='a23') # noqa: F841
        self.assertEqual(src1.media_source.all().count(), 3)

        self.assertEqual(src2.media_source.all().count(), 3)

        cleanup_old_media.call_local(durable=False)

        self.assertEqual(src1.media_source.all().count(), 3)
        self.assertEqual(src2.media_source.all().count(), 3)
        self.assertEqual(Media.objects.filter(pk=m22.pk).exists(), False)
        self.assertEqual(Media.objects.filter(source=src2, key=m22.key, skip=True).exists(), True)


class IndexOnlySkipMetadataRuntimeGuardTestCase(TestCase):
    '''
        Covers the runtime guard at the top of download_media_metadata()
        and download_media_image(): a task that was already queued before
        a source flipped to index-only (or that index_source() scheduled
        directly for a brand-new item on an index-only source, bypassing
        media_post_save()'s own scheduling gate) must become a silent
        no-op instead of touching YouTube -- and must not raise, retry, or
        get marked failed.
    '''

    def setUp(self):
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_download_media_metadata_is_a_noop_for_index_only_source(self):
        source = Source.objects.create(
            key='ro-meta', name='ro-meta', directory='/tmp/ro-meta',
            download_media=False,
        )
        media = Media.objects.create(source=source, key='vid1', title='Vid 1', published=timezone.now())
        with patch.object(Media, 'index_metadata') as mocked_index:
            result = download_media_metadata.call_local(str(media.pk))
        mocked_index.assert_not_called()
        self.assertIsNone(result)
        media.refresh_from_db()
        self.assertFalse(media.has_metadata)

    def test_download_media_image_is_a_noop_for_index_only_source(self):
        source = Source.objects.create(
            key='ro-thumb', name='ro-thumb', directory='/tmp/ro-thumb',
            download_media=False,
        )
        media = Media.objects.create(source=source, key='vid2', title='Vid 2', published=timezone.now())
        with patch('sync.tasks.get_remote_image') as mocked_fetch:
            result = download_media_image.call_local(str(media.pk), media.thumbnail)
        mocked_fetch.assert_not_called()
        self.assertFalse(result)
        media.refresh_from_db()
        self.assertFalse(media.thumb_file_exists)

    def test_normal_source_metadata_task_still_hits_the_indexer(self):
        # Confirms the guard is specific to index-only sources, not a
        # blanket short-circuit.
        source = Source.objects.create(
            key='rw-meta', name='rw-meta', directory='/tmp/rw-meta',
            download_media=True,
        )
        media = Media.objects.create(source=source, key='vid3', title='Vid 3', published=timezone.now())
        fake_response = json.loads(all_test_metadata['minimal'])
        with patch.object(Media, 'index_metadata', return_value=fake_response) as mocked_index:
            download_media_metadata.call_local(str(media.pk))
        mocked_index.assert_called_once()

    @override_settings(INDEX_ONLY_SKIP_METADATA=False)
    def test_setting_disabled_restores_upstream_metadata_fetch(self):
        source = Source.objects.create(
            key='ro-legacy-meta', name='ro-legacy-meta', directory='/tmp/ro-legacy-meta',
            download_media=False,
        )
        media = Media.objects.create(source=source, key='vid4', title='Vid 4', published=timezone.now())
        fake_response = json.loads(all_test_metadata['minimal'])
        with patch.object(Media, 'index_metadata', return_value=fake_response) as mocked_index:
            download_media_metadata.call_local(str(media.pk))
        mocked_index.assert_called_once()

    @override_settings(INDEX_ONLY_SKIP_METADATA=False)
    def test_setting_disabled_restores_upstream_thumbnail_fetch(self):
        source = Source.objects.create(
            key='ro-legacy-thumb', name='ro-legacy-thumb', directory='/tmp/ro-legacy-thumb',
            download_media=False,
        )
        media = Media.objects.create(source=source, key='vid5', title='Vid 5', published=timezone.now())
        # A real (tiny) image, not a bare MagicMock -- download_media_image()
        # compares its .width/.height to the resize thresholds and then
        # actually calls .save() on it.
        fake_image = Image.new('RGB', (16, 16))
        with patch('sync.tasks.get_remote_image', return_value=fake_image) as mocked_fetch:
            download_media_image.call_local(str(media.pk), media.thumbnail)
        mocked_fetch.assert_called_once()
