'''
    Covers MediaRedownloadView.form_valid()'s thumbnail-refetch behavior:
    an index-only source's media never gets its thumbnail re-fetched
    automatically (media_post_save()'s own scheduling is skipped for it
    -- see settings.INDEX_ONLY_SKIP_METADATA), so without this it would
    be lost for good the moment a manual redownload/fetch-metadata
    request deletes the existing thumbnail file.
'''
import io
import logging
import uuid
from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone
from common.models import TaskHistory
from sync.choices import Val, YouTube_SourceType
from sync.models import Source, Media
from sync.tasks import get_model_tasks


def make_source(**overrides):
    defaults = dict(
        source_type=Val(YouTube_SourceType.CHANNEL),
        key='UC_redownload_view',
        name='Redownload View Source',
        directory='/tmp/redownload_view',
    )
    defaults.update(overrides)
    return Source.objects.create(**defaults)


def make_media_with_thumb(source, **overrides):
    defaults = dict(
        source=source,
        key='video1',
        title='Test Video',
        published=timezone.now(),
    )
    defaults.update(overrides)
    media = Media.objects.create(**defaults)
    image_bytes = io.BytesIO()
    Image.new('RGB', (16, 16)).save(image_bytes, 'JPEG')
    media.thumb.save('thumb.jpg', SimpleUploadedFile('thumb.jpg', image_bytes.getvalue()), save=True)
    return media


class MediaRedownloadThumbnailRefetchTestCase(TestCase):

    def setUp(self):
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def _post_redownload(self, media):
        return Client().post(reverse('sync:redownload-media', kwargs={'pk': media.pk}))

    def test_index_only_thumbnail_refetch_scheduled_when_deleted(self):
        source = make_source(download_media=False)
        media = make_media_with_thumb(source)
        self.assertTrue(media.thumb_file_exists)

        response = self._post_redownload(media)
        self.assertEqual(response.status_code, 302)

        media.refresh_from_db()
        self.assertFalse(media.thumb)
        tasks = get_model_tasks(str(media.pk), name='download_media_image')
        self.assertEqual(tasks.count(), 1)
        self.assertIn("'manual': True", tasks.first().task_params[1])

    def test_not_duplicated_when_an_incomplete_fetch_already_exists(self):
        source = make_source(key='UC_dedupe', directory='/tmp/dedupe', download_media=False)
        media = make_media_with_thumb(source, key='video2')
        # A pending (never-started) thumbnail task already exists.
        TaskHistory.objects.create(
            task_id=str(uuid.uuid4()),
            name='sync.tasks.download_media_image',
            task_params=[[str(media.pk), media.thumbnail], '{}'],
            start_at=None,
            end_at=timezone.now(),
            scheduled_at=timezone.now(),
        )

        response = self._post_redownload(media)
        self.assertEqual(response.status_code, 302)

        # No second task was added.
        self.assertEqual(
            get_model_tasks(str(media.pk), name='download_media_image').count(), 1,
        )

    def test_not_scheduled_for_a_normal_source(self):
        # media_post_save() (triggered by self.object.save() later in
        # form_valid()) already schedules an ordinary, non-manual
        # thumbnail fetch for a normal source once its thumb is cleared
        # -- that pre-existing automatic path is untouched. This test
        # only guards against the NEW manual=True fetch also firing here.
        source = make_source(key='UC_normal', directory='/tmp/normal', download_media=True)
        media = make_media_with_thumb(source, key='video3')

        response = self._post_redownload(media)
        self.assertEqual(response.status_code, 302)

        tasks = get_model_tasks(str(media.pk), name='download_media_image')
        self.assertFalse(any("'manual': True" in (t.task_params[1] or '') for t in tasks))

    @override_settings(INDEX_ONLY_SKIP_METADATA=False)
    def test_not_scheduled_when_setting_disabled(self):
        # With the setting off, media_post_save()'s own automatic
        # thumbnail scheduling also applies to an index-only source
        # (restoring upstream behavior) -- same caveat as above.
        source = make_source(key='UC_legacy', directory='/tmp/legacy', download_media=False)
        media = make_media_with_thumb(source, key='video4')

        response = self._post_redownload(media)
        self.assertEqual(response.status_code, 302)

        tasks = get_model_tasks(str(media.pk), name='download_media_image')
        self.assertFalse(any("'manual': True" in (t.task_params[1] or '') for t in tasks))
