'''
    Unit tests for the pure Source/Media -> contract-schema mapping in
    mapping.py. No bridge URL, view, or auth code is exercised here --
    these tests build real ORM rows via sync.models and call the mapping
    functions directly, independent of how T2's URL-exemption question is
    resolved.
'''
import uuid
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from common.models import TaskHistory
from sync.choices import IndexSchedule, Val, YouTube_SourceType
from sync.models import Media, Source

from .. import mapping


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
        published=timezone.now(),
    )
    defaults.update(overrides)
    return Media.objects.create(**defaults)


def make_download_task(media, **overrides):
    '''
        Builds a download_media_file TaskHistory row with the same shape
        common.models.tasks.th_schedule() produces, for a chosen point in
        the task's own lifecycle:

          running: start_at == end_at, scheduled_at in the past.
          pending: start_at is None (not started yet), scheduled_at in
                    the future (a delayed task).
          failed: start_at != end_at (it ran to completion), failed_at
                   set, last_error populated.

        overrides win over these defaults, so a caller can build any
        combination (e.g. a delayed retry after a failure).
    '''
    now = timezone.now()
    defaults = dict(
        name='sync.tasks.download_media_file',
        task_id=str(uuid.uuid4()),
        task_params=[[str(media.pk)], '{}'],
        start_at=now,
        scheduled_at=now,
        end_at=now,
    )
    defaults.update(overrides)
    return TaskHistory.objects.create(**defaults)


class SourceTypeMappingTestCase(TestCase):

    def test_channel_by_handle_maps_to_channel(self):
        self.assertEqual(mapping.map_source_type(Val(YouTube_SourceType.CHANNEL)), 'channel')

    def test_channel_by_id_maps_to_channel(self):
        self.assertEqual(mapping.map_source_type(Val(YouTube_SourceType.CHANNEL_ID)), 'channel')

    def test_playlist_maps_to_playlist(self):
        self.assertEqual(mapping.map_source_type(Val(YouTube_SourceType.PLAYLIST)), 'playlist')


class SourceStateMappingTestCase(TestCase):

    def test_failed_takes_precedence_over_everything(self):
        source = make_source(has_failed=True, index_schedule=Val(IndexSchedule.NEVER))
        self.assertEqual(mapping.map_source_state(source), 'failed')

    def test_disabled_when_not_active(self):
        source = make_source(index_schedule=Val(IndexSchedule.NEVER))
        self.assertFalse(source.is_active)
        self.assertEqual(mapping.map_source_state(source), 'disabled')

    def test_provisioning_when_active_and_never_crawled(self):
        source = make_source()
        self.assertTrue(source.is_active)
        self.assertIsNone(source.last_crawl)
        self.assertEqual(mapping.map_source_state(source), 'provisioning')

    def test_active_when_crawled_and_not_failed(self):
        source = make_source(last_crawl=timezone.now())
        self.assertEqual(mapping.map_source_state(source), 'active')


class SerializeSourceTestCase(TestCase):

    def test_shape_and_field_mapping(self):
        source = make_source(last_crawl=timezone.now())
        body = mapping.serialize_source(source)
        self.assertEqual(body['uuid'], str(source.pk))
        self.assertEqual(body['sourceType'], 'channel')
        self.assertEqual(body['canonicalKey'], source.key)
        self.assertEqual(body['canonicalUrl'], source.url)
        self.assertEqual(body['name'], source.name)
        self.assertEqual(body['directory'], source.directory)
        self.assertEqual(body['normalizedState'], 'active')
        self.assertIsNotNone(body['lastCrawlAt'])
        self.assertIn('hasFailed', body['rawState'])
        self.assertIn('isActive', body['rawState'])
        self.assertIn('indexTaskRunning', body['rawState'])

    def test_never_crawled_last_crawl_at_is_null(self):
        source = make_source()
        body = mapping.serialize_source(source)
        self.assertIsNone(body['lastCrawlAt'])

    def test_index_task_snapshot_is_consistent_between_raw_and_normalized(self):
        source = make_source(last_crawl=timezone.now())
        with patch('medianest_bridge.mapping.get_source_index_task', return_value=True):
            body = mapping.serialize_source(source)
        self.assertTrue(body['rawState']['indexTaskRunning'])
        self.assertEqual(body['normalizedState'], 'syncing')
        with patch('medianest_bridge.mapping.get_source_index_task', return_value=False):
            body = mapping.serialize_source(source)
        self.assertFalse(body['rawState']['indexTaskRunning'])
        self.assertEqual(body['normalizedState'], 'active')


class MediaStateMappingTestCase(TestCase):

    def test_unknown_maps_to_discovered_not_unknown(self):
        # Deliberate: TubeSync's UNKNOWN is well-determined (indexed, no
        # work item yet) -- not a case this bridge can't figure out.
        from sync.choices import MediaState
        self.assertEqual(mapping.map_media_state(Val(MediaState.UNKNOWN)), 'discovered')

    def test_full_state_map(self):
        from sync.choices import MediaState
        expected = {
            MediaState.SCHEDULED: 'queued',
            MediaState.DOWNLOADING: 'downloading',
            MediaState.DOWNLOADED: 'downloaded',
            MediaState.SKIPPED: 'skipped',
            MediaState.DISABLED_AT_SOURCE: 'ineligible',
            MediaState.ERROR: 'failed',
        }
        for raw, normalized in expected.items():
            self.assertEqual(mapping.map_media_state(Val(raw)), normalized)

    def test_unrecognized_raw_value_falls_back_to_unknown(self):
        self.assertEqual(mapping.map_media_state('not-a-real-state'), 'unknown')


class SerializeMediaTestCase(TestCase):

    def test_shape_and_field_mapping_for_undownloaded_media(self):
        source = make_source()
        media = make_media(source)
        body = mapping.serialize_media(media)
        self.assertEqual(body['id'], str(media.pk))
        self.assertEqual(body['sourceId'], str(source.pk))
        self.assertEqual(body['youtubeKey'], 'video1')
        self.assertEqual(body['title'], 'Test Video')
        self.assertIsNotNone(body['publishedAt'])
        self.assertIsNone(body['relativePath'])
        self.assertIsNone(body['filename'])
        self.assertIsNone(body['downloadedAt'])
        self.assertIsNone(body['retryAt'])
        self.assertIsNone(body['error'])
        self.assertFalse(body['eligible'])  # can_download defaults to False

    def test_downloaded_media_reports_relative_path_and_filename(self):
        source = make_source()
        media = make_media(source, downloaded=True, downloaded_filesize=12345)
        media.media_file.name = f'{source.directory}/video1.mp4'
        media.save(update_fields={'media_file'})
        body = mapping.serialize_media(media)
        self.assertEqual(body['relativePath'], f'{source.directory}/video1.mp4')
        self.assertEqual(body['filename'], 'video1.mp4')
        self.assertEqual(body['sizeBytes'], 12345)
        self.assertEqual(body['normalizedState'], 'downloaded')

    def test_skipped_media_maps_to_skipped(self):
        # manual_skip=True is required alongside skip=True: sync/signals.py
        # media_post_save() recalculates `skip` on every save and bails out
        # of that recalculation only when manual_skip is set, so a plain
        # skip=True without manual_skip does not reliably survive .save().
        source = make_source()
        media = make_media(source, skip=True, manual_skip=True)
        body = mapping.serialize_media(media)
        self.assertEqual(body['normalizedState'], 'skipped')

    def test_eligible_reflects_can_download(self):
        source = make_source()
        media = make_media(source, can_download=True)
        body = mapping.serialize_media(media)
        self.assertTrue(body['eligible'])


class SerializeMediaTaskStateTestCase(TestCase):
    '''
        T2 review P1 finding: get_media_download_task()'s running-only
        predicate silently drops a delayed-but-not-yet-started task and a
        terminal failure alike, so both used to fall through to
        "discovered" instead of "queued"/"failed". These exercise
        serialize_media()'s default (download_task=_MISSING) path, which
        now goes through get_relevant_media_download_task() instead.
    '''

    def test_pending_not_yet_started_task_maps_to_queued(self):
        source = make_source()
        media = make_media(source)
        future = timezone.now() + timezone.timedelta(seconds=60)
        make_download_task(media, start_at=None, scheduled_at=future)
        body = mapping.serialize_media(media)
        self.assertEqual(body['normalizedState'], 'queued')
        self.assertIsNone(body['retryAt'])
        self.assertIsNone(body['error'])

    def test_terminally_failed_task_maps_to_failed_with_retry_and_error(self):
        source = make_source()
        media = make_media(source)
        past = timezone.now() - timezone.timedelta(seconds=30)
        retry_at = timezone.now() + timezone.timedelta(seconds=300)
        make_download_task(
            media,
            start_at=past,
            end_at=timezone.now(),
            scheduled_at=retry_at,
            failed_at=timezone.now(),
            last_error='RuntimeError: network unreachable',
        )
        body = mapping.serialize_media(media)
        self.assertEqual(body['normalizedState'], 'failed')
        self.assertIsNotNone(body['retryAt'])
        self.assertEqual(body['error'], 'network unreachable')

    def test_running_task_maps_to_downloading(self):
        source = make_source()
        media = make_media(source)
        make_download_task(media)  # defaults are the running() shape
        body = mapping.serialize_media(media)
        self.assertEqual(body['normalizedState'], 'downloading')

    def test_no_task_at_all_maps_to_discovered(self):
        source = make_source()
        media = make_media(source)
        body = mapping.serialize_media(media)
        self.assertEqual(body['normalizedState'], 'discovered')

    def test_successfully_completed_task_does_not_mask_discovered_state(self):
        # A plain successful row (no failure, already finished) must not
        # be picked up by the fallback tier -- Media.downloaded already
        # short-circuits get_download_state() before any task matters in
        # that case, and this media was never actually downloaded here.
        source = make_source()
        media = make_media(source)
        past = timezone.now() - timezone.timedelta(seconds=30)
        make_download_task(media, start_at=past, end_at=timezone.now())
        body = mapping.serialize_media(media)
        self.assertEqual(body['normalizedState'], 'discovered')


class BatchMediaDownloadTasksTestCase(TestCase):

    def test_running_task_found_via_first_tier(self):
        source = make_source()
        media = make_media(source)
        task = make_download_task(media)
        result = mapping.batch_media_download_tasks([media.pk])
        self.assertEqual(result[str(media.pk)].pk, task.pk)

    def test_pending_task_found_via_fallback_tier(self):
        source = make_source()
        media = make_media(source)
        future = timezone.now() + timezone.timedelta(seconds=60)
        task = make_download_task(media, start_at=None, scheduled_at=future)
        result = mapping.batch_media_download_tasks([media.pk])
        self.assertEqual(result[str(media.pk)].pk, task.pk)

    def test_failed_task_found_via_fallback_tier(self):
        source = make_source()
        media = make_media(source)
        task = make_download_task(
            media,
            failed_at=timezone.now(),
            last_error='RuntimeError: boom',
        )
        result = mapping.batch_media_download_tasks([media.pk])
        self.assertEqual(result[str(media.pk)].pk, task.pk)

    def test_media_with_no_relevant_task_is_false(self):
        source = make_source()
        media = make_media(source)
        result = mapping.batch_media_download_tasks([media.pk])
        self.assertFalse(result[str(media.pk)])

    def test_batch_distinguishes_multiple_media_by_id(self):
        source = make_source()
        running_media = make_media(source, key='running')
        pending_media = make_media(source, key='pending')
        idle_media = make_media(source, key='idle')
        running_task = make_download_task(running_media)
        future = timezone.now() + timezone.timedelta(seconds=60)
        pending_task = make_download_task(pending_media, start_at=None, scheduled_at=future)
        result = mapping.batch_media_download_tasks(
            [running_media.pk, pending_media.pk, idle_media.pk],
        )
        self.assertEqual(result[str(running_media.pk)].pk, running_task.pk)
        self.assertEqual(result[str(pending_media.pk)].pk, pending_task.pk)
        self.assertFalse(result[str(idle_media.pk)])
