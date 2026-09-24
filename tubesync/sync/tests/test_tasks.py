import json
import logging
import uuid
from collections import deque
from datetime import timedelta
from unittest.mock import patch
from PIL import Image
from django.test import TestCase, override_settings
from django.utils import timezone
from common.models import TaskHistory
from sync.choices import (
    Fallback, SourceResolution, Val, YouTube_AudioCodec, YouTube_SourceType, YouTube_VideoCodec,
)
from sync.models import Source, Media
from sync.tasks import (
    cleanup_old_media,
    download_media_image,
    download_media_metadata,
    get_model_tasks,
    index_source,
)
from .fixtures import all_test_metadata


def download_task_has_override(media):
    # Mirrors medianest_bridge/mapping.py's _task_has_override(): task_params[1]
    # is repr(task_obj.kwargs), a string, not a real dict.
    tasks = get_model_tasks(str(media.pk), name='download_media_file')
    return any("'override': True" in (t.task_params[1] or '') for t in tasks)


def make_download_media_file_task(media, *, start_at, end_at=None, failed_at=None):
    '''
        Builds a raw download_media_file TaskHistory row with a chosen
        lifecycle state, matching common/models/tasks.py::th_schedule()'s
        and common/huey.py::historical_task()'s own shape:

          pending (never run):  start_at=None
          running:               start_at == end_at
          finished (success or
          failure-without-retry): start_at is set and start_at != end_at

        (see has_incomplete_task()'s docstring in sync/tasks.py).
    '''
    now = timezone.now()
    return TaskHistory.objects.create(
        task_id=str(uuid.uuid4()),
        name='sync.tasks.download_media_file',
        task_params=[[str(media.pk)], '{}'],
        start_at=start_at,
        end_at=end_at if end_at is not None else now,
        scheduled_at=now,
        failed_at=failed_at,
    )

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


class ManualActionBypassesIndexOnlyGuardTestCase(TestCase):
    '''
        A user-initiated action (MediaRedownloadView's redownload
        confirmation, MediaItemView's thumbnail-redownload action) must
        still work for an index-only source's media -- the rc9 gate's
        step 6 proof manually redownloads one item from an index-only
        pilot source via media-redownload. manual=True on
        download_media_metadata()/download_media_image() is how those
        views ask for that, bypassing the automatic-scheduling-only
        index-only guard.
    '''

    def setUp(self):
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_manual_metadata_fetch_bypasses_guard_and_schedules_download(self):
        source = Source.objects.create(
            key='ro-manual-meta', name='ro-manual-meta', directory='/tmp/ro-manual-meta',
            download_media=False,
            source_resolution=Val(SourceResolution.VIDEO_1080P),
            source_vcodec=Val(YouTube_VideoCodec.VP9),
            source_acodec=Val(YouTube_AudioCodec.OPUS),
            prefer_60fps=False,
            prefer_hdr=False,
            fallback=Val(Fallback.FAIL),
        )
        media = Media.objects.create(
            source=source, key='vid-manual-1', title='Vid Manual 1', published=timezone.now(),
        )
        self.assertFalse(media.has_metadata)
        self.assertFalse(media.can_download)

        fake_response = json.loads(all_test_metadata['minimal'])
        with patch.object(Media, 'index_metadata', return_value=fake_response) as mocked_index:
            download_media_metadata.call_local(str(media.pk), manual=True)
        mocked_index.assert_called_once()

        media.refresh_from_db()
        self.assertTrue(media.has_metadata)
        self.assertTrue(media.can_download)
        # download_media_metadata(manual=True), on success, explicitly
        # schedules download_media_file(override=True) itself -- the
        # automatic scheduling in media_post_save() never would, since
        # source.download_media is still False.
        self.assertTrue(download_task_has_override(media))

    def test_manual_metadata_fetch_on_normal_source_is_unaffected(self):
        # The manual kwarg must not change behavior for a normal source:
        # media_post_save()'s own automatic download_media_file scheduling
        # already covers it, so download_media_metadata() must not
        # double-schedule an extra override=True task.
        source = Source.objects.create(
            key='rw-manual-meta', name='rw-manual-meta', directory='/tmp/rw-manual-meta',
            download_media=True,
            source_resolution=Val(SourceResolution.VIDEO_1080P),
            source_vcodec=Val(YouTube_VideoCodec.VP9),
            source_acodec=Val(YouTube_AudioCodec.OPUS),
            prefer_60fps=False,
            prefer_hdr=False,
            fallback=Val(Fallback.FAIL),
        )
        media = Media.objects.create(
            source=source, key='vid-manual-2', title='Vid Manual 2', published=timezone.now(),
        )
        fake_response = json.loads(all_test_metadata['minimal'])
        with patch.object(Media, 'index_metadata', return_value=fake_response) as mocked_index:
            download_media_metadata.call_local(str(media.pk), manual=True)
        mocked_index.assert_called_once()
        media.refresh_from_db()
        self.assertTrue(media.can_download)
        # media_post_save()'s own automatic scheduling already covers this
        # (source.download_media is True): no override=True task exists,
        # only the plain automatic one.
        self.assertFalse(download_task_has_override(media))
        self.assertTrue(get_model_tasks(str(media.pk), name='download_media_file').exists())

    def _make_index_only_source_and_media(self, *, key_suffix):
        source = Source.objects.create(
            key=f'ro-manual-chain-{key_suffix}', name=f'ro-manual-chain-{key_suffix}',
            directory=f'/tmp/ro-manual-chain-{key_suffix}',
            download_media=False,
            source_resolution=Val(SourceResolution.VIDEO_1080P),
            source_vcodec=Val(YouTube_VideoCodec.VP9),
            source_acodec=Val(YouTube_AudioCodec.OPUS),
            prefer_60fps=False,
            prefer_hdr=False,
            fallback=Val(Fallback.FAIL),
        )
        media = Media.objects.create(
            source=source, key=f'vid-manual-chain-{key_suffix}',
            title=f'Vid Manual Chain {key_suffix}', published=timezone.now(),
        )
        return source, media

    def test_manual_download_chain_ignores_a_finished_download_task(self):
        # A download_media_file row from an earlier, now-finished attempt
        # (succeeded, or failed with no retry pending: start_at is set
        # and no longer equal to end_at) is kept for
        # COMPLETED_TASKS_DAYS_TO_KEEP days and must not block scheduling
        # a fresh manual download.
        source, media = self._make_index_only_source_and_media(key_suffix='finished')
        past = timezone.now() - timedelta(days=1)
        make_download_media_file_task(media, start_at=past, end_at=past + timedelta(minutes=5))

        fake_response = json.loads(all_test_metadata['minimal'])
        with patch.object(Media, 'index_metadata', return_value=fake_response):
            download_media_metadata.call_local(str(media.pk), manual=True)

        media.refresh_from_db()
        self.assertTrue(media.can_download)
        self.assertTrue(download_task_has_override(media))

    def test_manual_download_chain_ignores_a_failed_download_task(self):
        source, media = self._make_index_only_source_and_media(key_suffix='failed')
        past = timezone.now() - timedelta(days=1)
        failed_at = past + timedelta(minutes=5)
        make_download_media_file_task(media, start_at=past, end_at=failed_at, failed_at=failed_at)

        fake_response = json.loads(all_test_metadata['minimal'])
        with patch.object(Media, 'index_metadata', return_value=fake_response):
            download_media_metadata.call_local(str(media.pk), manual=True)

        media.refresh_from_db()
        self.assertTrue(media.can_download)
        self.assertTrue(download_task_has_override(media))

    def test_manual_download_chain_is_blocked_by_a_pending_download_task(self):
        source, media = self._make_index_only_source_and_media(key_suffix='pending')
        # Pending: never started (start_at is None).
        make_download_media_file_task(media, start_at=None)
        self.assertEqual(
            get_model_tasks(str(media.pk), name='download_media_file').count(), 1,
        )

        fake_response = json.loads(all_test_metadata['minimal'])
        with patch.object(Media, 'index_metadata', return_value=fake_response):
            download_media_metadata.call_local(str(media.pk), manual=True)

        media.refresh_from_db()
        self.assertTrue(media.can_download)
        # No second, override=True task was added on top of the existing
        # pending one.
        self.assertEqual(
            get_model_tasks(str(media.pk), name='download_media_file').count(), 1,
        )
        self.assertFalse(download_task_has_override(media))

    def test_manual_thumbnail_fetch_bypasses_guard(self):
        source = Source.objects.create(
            key='ro-manual-thumb', name='ro-manual-thumb', directory='/tmp/ro-manual-thumb',
            download_media=False,
        )
        media = Media.objects.create(
            source=source, key='vid-manual-3', title='Vid Manual 3', published=timezone.now(),
        )
        fake_image = Image.new('RGB', (16, 16))
        with patch('sync.tasks.get_remote_image', return_value=fake_image) as mocked_fetch:
            result = download_media_image.call_local(str(media.pk), media.thumbnail, manual=True)
        mocked_fetch.assert_called_once()
        self.assertTrue(result)
        media.refresh_from_db()
        self.assertTrue(media.thumb_file_exists)

    def test_manual_metadata_fetch_chains_download_despite_completed_history(self):
        source = Source.objects.create(
            key='ro-manual-retry', name='ro-manual-retry', directory='/tmp/ro-manual-retry',
            download_media=False,
            source_resolution=Val(SourceResolution.VIDEO_1080P),
            source_vcodec=Val(YouTube_VideoCodec.VP9),
            source_acodec=Val(YouTube_AudioCodec.OPUS),
            prefer_60fps=False,
            prefer_hdr=False,
            fallback=Val(Fallback.FAIL),
        )
        media = Media.objects.create(
            source=source, key='vid-manual-4', title='Vid Manual 4', published=timezone.now(),
        )
        now = timezone.now()
        TaskHistory.objects.create(
            name='sync.tasks.download_media_file',
            task_id=str(uuid.uuid4()),
            task_params=[[str(media.pk)], "{'override': True}"],
            start_at=now - timedelta(hours=1),
            scheduled_at=now - timedelta(hours=1),
            end_at=now,
            failed_at=now,
            last_error='previous attempt failed',
        )

        fake_response = json.loads(all_test_metadata['minimal'])
        with patch.object(Media, 'index_metadata', return_value=fake_response):
            download_media_metadata.call_local(str(media.pk), manual=True)

        self.assertTrue(download_task_has_override(media))


class IndexSourceSkipsIndexOnlyFetchTestCase(TestCase):
    '''
        index_source() schedules metadata/thumbnail fetches for a
        brand-new item directly (bypassing media_post_save()'s own
        scheduling gate entirely), unconditional on source.download_media
        before this fix. It must not create those TaskHistory rows at all
        for an index-only source, matching the scheduling gate in
        media_post_save().
    '''

    def setUp(self):
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def _run_index_with_one_fake_video(self, source):
        fake_video = {
            'id': 'newvid1',
            'duration': 120,
            'title': 'New Video',
            'ie_key': 'Youtube',
            'timestamp': int(timezone.now().timestamp()),
        }
        with patch.object(Source, 'index_media', return_value=deque([fake_video])):
            index_source.call_local(str(source.pk))
        return Media.objects.get(source=source, key='newvid1')

    def test_index_only_source_creates_no_fetch_tasks_for_new_media(self):
        source = Source.objects.create(
            key='ro-index', name='ro-index', directory='/tmp/ro-index',
            source_type=Val(YouTube_SourceType.CHANNEL),
            download_media=False,
        )
        media = self._run_index_with_one_fake_video(source)
        self.assertFalse(get_model_tasks(str(media.pk), name='download_media_metadata').exists())
        self.assertFalse(get_model_tasks(str(media.pk), name='download_media_image').exists())
        # Title/duration/published still come through from the index
        # listing itself, independent of any per-item metadata fetch.
        self.assertEqual(media.title, 'New Video')
        self.assertEqual(media.duration, 120)
        self.assertIsNotNone(media.published)

    def test_normal_source_still_creates_fetch_tasks_for_new_media(self):
        source = Source.objects.create(
            key='rw-index', name='rw-index', directory='/tmp/rw-index',
            source_type=Val(YouTube_SourceType.CHANNEL),
            download_media=True,
        )
        media = self._run_index_with_one_fake_video(source)
        self.assertTrue(get_model_tasks(str(media.pk), name='download_media_metadata').exists())
        self.assertTrue(get_model_tasks(str(media.pk), name='download_media_image').exists())

    @override_settings(INDEX_ONLY_SKIP_METADATA=False)
    def test_setting_disabled_restores_upstream_index_scheduling(self):
        source = Source.objects.create(
            key='ro-index-legacy', name='ro-index-legacy', directory='/tmp/ro-index-legacy',
            source_type=Val(YouTube_SourceType.CHANNEL),
            download_media=False,
        )
        media = self._run_index_with_one_fake_video(source)
        self.assertTrue(get_model_tasks(str(media.pk), name='download_media_metadata').exists())
        self.assertTrue(get_model_tasks(str(media.pk), name='download_media_image').exists())
