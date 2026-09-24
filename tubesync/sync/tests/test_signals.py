import logging
from django.test import TestCase, override_settings
from django.utils import timezone
from sync.choices import Val, YouTube_SourceType
from sync.models import Source, Media
from sync.tasks import get_model_tasks, save_all_media_for_source, save_media


def make_source(**overrides):
    defaults = dict(
        source_type=Val(YouTube_SourceType.CHANNEL),
        key='UC_test_channel',
        name='Test Channel',
        directory='test_channel',
    )
    defaults.update(overrides)
    return Source.objects.create(**defaults)


def make_media(source, **overrides):
    defaults = dict(
        source=source,
        key='video1',
        title='Test Video',
        # filter_media() (sync/filtering.py) marks unpublished media as
        # skip=True regardless of source.download_media, which would
        # otherwise mask whether the index-only gate under test actually
        # did anything.
        published=timezone.now(),
    )
    defaults.update(overrides)
    return Media.objects.create(**defaults)


def metadata_task_exists(media):
    # get_media_metadata_task() only matches "running" TaskHistory rows
    # (start_at == end_at), which a freshly-scheduled-but-not-started task
    # never is -- get_model_tasks() matches on name/task_params alone, so
    # it reliably reflects whether TaskHistory.schedule() was ever called
    # for this media, regardless of whether huey has picked it up yet.
    return get_model_tasks(str(media.pk), name='download_media_metadata').exists()


def thumbnail_task_exists(media):
    return get_model_tasks(str(media.pk), name='download_media_image').exists()


def save_all_media_for_source_task_exists(source):
    return get_model_tasks(str(source.pk), name='save_all_media_for_source').exists()


class IndexOnlySkipMetadataTestCase(TestCase):
    '''
        media_post_save() (sync/signals.py) must not schedule per-item
        metadata or thumbnail downloads for media belonging to an
        index-only source (Source.download_media=False), unless the fork
        setting settings.INDEX_ONLY_SKIP_METADATA is turned off.
    '''

    def setUp(self):
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_index_only_source_skips_metadata_and_thumbnail(self):
        source = make_source(download_media=False)
        media = make_media(source)
        self.assertFalse(metadata_task_exists(media))
        self.assertFalse(thumbnail_task_exists(media))

    def test_normal_source_still_schedules_metadata_and_thumbnail(self):
        source = make_source(key='UC_normal', directory='normal', download_media=True)
        media = make_media(source, key='video2')
        self.assertTrue(metadata_task_exists(media))
        self.assertTrue(thumbnail_task_exists(media))

    @override_settings(INDEX_ONLY_SKIP_METADATA=False)
    def test_setting_disabled_restores_upstream_behavior(self):
        source = make_source(key='UC_legacy', directory='legacy', download_media=False)
        media = make_media(source, key='video3')
        self.assertTrue(metadata_task_exists(media))
        self.assertTrue(thumbnail_task_exists(media))

    def test_turning_download_media_back_on_reschedules_metadata(self):
        '''
            Exercises the real production chain rather than calling
            media.save() directly: source.save() -> source_post_save()
            unconditionally schedules save_all_media_for_source() ->
            (run synchronously here) -> save_media.map(...) -> (run
            synchronously here, matching what huey would eventually do
            per queued media) -> save_media() -> media.save() ->
            media_post_save(), now with the index-only gate open.
        '''
        source = make_source(key='UC_flip', directory='flip', download_media=False)
        media = make_media(source, key='video4')
        self.assertFalse(metadata_task_exists(media))
        self.assertFalse(thumbnail_task_exists(media))

        source.download_media = True
        source.save()
        self.assertTrue(save_all_media_for_source_task_exists(source))

        save_all_media_for_source.call_local(str(source.pk))
        save_media.call_local(str(media.pk))

        self.assertTrue(metadata_task_exists(media))
        self.assertTrue(thumbnail_task_exists(media))
