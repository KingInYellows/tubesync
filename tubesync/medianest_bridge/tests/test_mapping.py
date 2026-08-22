'''
    Unit tests for the pure Source/Media -> contract-schema mapping in
    mapping.py. No bridge URL, view, or auth code is exercised here --
    these tests build real ORM rows via sync.models and call the mapping
    functions directly, independent of how T2's URL-exemption question is
    resolved.
'''
from django.test import TestCase
from django.utils import timezone

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

    def test_error_field_is_sanitized_end_to_end(self):
        '''
            T4: confirms error_sanitize.sanitize_error_message() is
            actually wired into serialize_media(), not just unit-tested
            in isolation -- constructs a real TaskHistory row matching
            get_media_download_task()'s exact lookup requirements (name
            ending in download_media_file, task_params starting with
            the media pk, start_at == end_at so TaskHistoryQuerySet.
            running() finds it) with a path-bearing last_error, and
            asserts the path never reaches serialize_media()'s output.
        '''
        from django.conf import settings

        from common.models import TaskHistory

        source = make_source()
        media = make_media(source)
        now = timezone.now()
        TaskHistory.objects.create(
            name='sync.tasks.download_media_file',
            task_id='test-task-id-error-sanitize',
            task_params=[[str(media.pk)], '{}'],
            start_at=now,
            end_at=now,
            scheduled_at=now,
            failed_at=now,
            last_error=f"[Errno 2] No such file or directory: '{settings.DOWNLOAD_ROOT}/leaked/path.mp4'",
        )
        body = mapping.serialize_media(media)
        self.assertIsNotNone(body['error'])
        self.assertNotIn(str(settings.DOWNLOAD_ROOT), body['error'])
        self.assertNotIn('leaked', body['error'])
        self.assertIn('<redacted>', body['error'])
