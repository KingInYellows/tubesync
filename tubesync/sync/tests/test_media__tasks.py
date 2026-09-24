'''
    Covers sync/models/media__tasks.py::copy_thumbnail()'s propagation of
    manual=True to its internal download_media_image.call_local() call --
    a codex P2 finding: for an index-only source with copy_thumbnails=True,
    that internal call didn't pass manual=True, so it was silently
    blocked by download_media_image()'s own index-only skip guard even
    right after a successful manual override download (the only way
    copy_thumbnail() is ever reached for an index-only source at all).
'''
import logging
from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from sync.choices import Val, YouTube_SourceType
from sync.models import Source, Media


class CopyThumbnailManualPropagationTestCase(TestCase):

    def setUp(self):
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_index_only_source_propagates_manual_true(self):
        source = Source.objects.create(
            source_type=Val(YouTube_SourceType.CHANNEL),
            key='UC_copy_thumb_ro', name='Copy Thumb RO', directory='/tmp/copy_thumb_ro',
            download_media=False, copy_thumbnails=True,
        )
        media = Media.objects.create(
            source=source, key='vid1', title='Vid 1', published=timezone.now(),
        )
        self.assertFalse(media.thumb_file_exists)

        with patch('sync.tasks.download_media_image') as mocked_task:
            mocked_task.call_local.return_value = False
            media.copy_thumbnail()

        mocked_task.call_local.assert_called_once_with(
            str(media.pk), media.thumbnail, manual=True,
        )

    def test_normal_source_also_propagates_manual_true(self):
        # manual=True is a no-op for a normal source (the index-only
        # guard only ever applies when source.download_media is False),
        # so passing it unconditionally here is safe either way -- this
        # just confirms the call site was actually changed, not that it
        # changes behavior for a normal source.
        source = Source.objects.create(
            source_type=Val(YouTube_SourceType.CHANNEL),
            key='UC_copy_thumb_rw', name='Copy Thumb RW', directory='/tmp/copy_thumb_rw',
            download_media=True, copy_thumbnails=True,
        )
        media = Media.objects.create(
            source=source, key='vid2', title='Vid 2', published=timezone.now(),
        )

        with patch('sync.tasks.download_media_image') as mocked_task:
            mocked_task.call_local.return_value = False
            media.copy_thumbnail()

        mocked_task.call_local.assert_called_once_with(
            str(media.pk), media.thumbnail, manual=True,
        )

    def test_does_nothing_when_copy_thumbnails_disabled(self):
        source = Source.objects.create(
            source_type=Val(YouTube_SourceType.CHANNEL),
            key='UC_copy_thumb_off', name='Copy Thumb Off', directory='/tmp/copy_thumb_off',
            download_media=False, copy_thumbnails=False,
        )
        media = Media.objects.create(
            source=source, key='vid3', title='Vid 3', published=timezone.now(),
        )

        with patch('sync.tasks.download_media_image') as mocked_task:
            media.copy_thumbnail()

        mocked_task.call_local.assert_not_called()
