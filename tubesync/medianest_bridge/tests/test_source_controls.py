'''
    Contract 1.6.0 (MED-809, MED-810, MED-813, MediaNest ADR-0036):
    PATCH /sources/{uuid} (pause, resume, schedule), POST
    /sources/{uuid}/media/{id}/retry and /skip, Source.settings and
    MediaItem.failureKind.
'''
import json
import tempfile
import uuid
from pathlib import Path
from unittest.mock import PropertyMock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from common.models import TaskHistory
from sync.choices import IndexSchedule, Val, YouTube_SourceType
from sync.models import Media, Source
from sync.tasks import download_media_file, download_media_metadata, refresh_formats

from .. import failure_kind, mapping, pause
from ..sync_dedup import find_pending_or_running_index_task
from .base import BridgeTestCase, BridgeTransactionTestCase
from .test_delete_sources import temp_download_root
from .test_endpoints import assert_matches_schema
from .test_mapping import make_download_task

API = '/api/medianest/v1'
KEY = 'UCabcdefghijklmnopqrstuv'
HOURLY = Val(IndexSchedule.EVERY_HOUR)
DAILY = Val(IndexSchedule.EVERY_24_HOURS)
WEEKLY = Val(IndexSchedule.EVERY_7_DAYS)
NEVER = Val(IndexSchedule.NEVER)


def source_url(source_uuid):
    return f'{API}/sources/{source_uuid}'


def media_url(source_uuid, media_id, action):
    return f'{API}/sources/{source_uuid}/media/{media_id}/{action}'


def make_source(**overrides):
    fields = dict(
        source_type=Val(YouTube_SourceType.CHANNEL_ID),
        key=KEY, name=f'acq-src-{KEY}', directory=f'acq-src-{KEY}',
        index_schedule=HOURLY,
    )
    fields.update(overrides)
    return Source.objects.create(**fields)


def make_media(source, **overrides):
    fields = dict(source=source, key='dQw4w9WgXcQ', title='A video', published=timezone.now())
    fields.update(overrides)
    return Media.objects.create(**fields)


class ControlsTestMixin:

    def setUp(self):
        super().setUp()
        self._config_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._config_dir.cleanup)
        settings_ctx = override_settings(CONFIG_BASE_DIR=Path(self._config_dir.name))
        settings_ctx.enable()
        self.addCleanup(settings_ctx.disable)

    def patch_json(self, url, body, **extra):
        return self.client.patch(
            url, data=json.dumps(body), content_type='application/json',
            **self.auth_header(), **extra,
        )

    def post(self, url):
        return self.client.post(url, **self.auth_header())

    def writable(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')


class PauseResumeTestCase(ControlsTestMixin, BridgeTestCase):

    def test_pause_stops_indexing_and_downloads(self):
        self.writable()
        source = make_source()
        response = self.patch_json(source_url(source.pk), {'paused': True})
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        assert_matches_schema(self, body, 'Source')
        assert_matches_schema(self, body['settings'], 'SourceSettings')
        self.assertEqual(body['normalizedState'], 'paused')
        self.assertTrue(body['settings']['paused'])
        self.assertEqual(body['settings']['indexSchedule'], HOURLY)
        self.assertFalse(body['settings']['downloadMedia'])
        self.assertIsNotNone(body['settings']['pausedAt'])
        source.refresh_from_db()
        self.assertEqual(source.index_schedule, NEVER)
        self.assertFalse(source.download_media)
        self.assertTrue(source.index_videos)
        self.assertFalse(source.is_active)

    def test_get_reports_settings(self):
        self.enable_bridge()
        source = make_source(index_schedule=WEEKLY)
        body = json.loads(self.client.get(source_url(source.pk), **self.auth_header()).content)
        self.assertEqual(body['settings'], {
            'paused': False, 'pausedAt': None,
            'indexSchedule': WEEKLY, 'downloadMedia': True,
        })

    def test_pause_twice_keeps_the_first_record(self):
        self.writable()
        source = make_source()
        first = json.loads(self.patch_json(source_url(source.pk), {'paused': True}).content)
        second = json.loads(self.patch_json(source_url(source.pk), {'paused': True}).content)
        self.assertEqual(first['settings'], second['settings'])
        self.assertEqual(second['settings']['indexSchedule'], HOURLY)

    def test_pause_of_a_source_set_to_never_index_in_tubesync_turns_downloads_off(self):
        # CodeRabbit review on #86: a source set to never index in
        # TubeSync's own UI already reads as paused, but its downloads are
        # still on until the bridge pauses it.
        self.writable()
        source = make_source(index_schedule=NEVER)
        body = json.loads(self.patch_json(source_url(source.pk), {'paused': True}).content)
        self.assertTrue(body['settings']['paused'])
        self.assertFalse(body['settings']['downloadMedia'])
        source.refresh_from_db()
        self.assertFalse(source.download_media)
        self.patch_json(source_url(source.pk), {'paused': False})
        source.refresh_from_db()
        self.assertTrue(source.download_media)
        self.assertEqual(source.index_schedule, pause.default_schedule(source))

    def test_a_source_that_indexes_nothing_can_still_be_resumed(self):
        # CodeRabbit review on #86.
        self.writable()
        source = make_source(index_videos=False, index_streams=False)
        body = json.loads(self.patch_json(source_url(source.pk), {'paused': True}).content)
        self.assertTrue(body['settings']['paused'])
        body = json.loads(self.patch_json(source_url(source.pk), {'paused': False}).content)
        self.assertFalse(body['settings']['paused'])
        source.refresh_from_db()
        self.assertEqual(source.index_schedule, HOURLY)
        self.assertTrue(source.download_media)

    def test_resume_restores_the_schedule_and_asks_for_an_index(self):
        self.writable()
        source = make_source()
        self.patch_json(source_url(source.pk), {'paused': True})
        TaskHistory.objects.all().delete()
        response = self.patch_json(source_url(source.pk), {'paused': False})
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        self.assertNotEqual(body['normalizedState'], 'paused')
        self.assertEqual(body['settings'], {
            'paused': False, 'pausedAt': None,
            'indexSchedule': HOURLY, 'downloadMedia': True,
        })
        source.refresh_from_db()
        self.assertEqual(source.index_schedule, HOURLY)
        self.assertTrue(source.download_media)
        self.assertFalse(list(pause.record_dir().glob('*.json')))
        self.assertIsNotNone(find_pending_or_running_index_task(str(source.pk)))

    def test_resume_keeps_a_source_that_did_not_download_index_only(self):
        self.writable()
        source = make_source(download_media=False)
        self.patch_json(source_url(source.pk), {'paused': True})
        self.patch_json(source_url(source.pk), {'paused': False})
        source.refresh_from_db()
        self.assertFalse(source.download_media)

    def test_resume_with_no_record_uses_the_default_schedule(self):
        self.enable_bridge(
            MEDIANEST_BRIDGE_READ_ONLY='false', MEDIANEST_BRIDGE_SOURCE_DEFAULTS='{}',
        )
        # Set to "never" in TubeSync's own UI: paused, with no record.
        source = make_source(index_schedule=NEVER, download_media=False)
        body = json.loads(self.client.get(source_url(source.pk), **self.auth_header()).content)
        self.assertEqual(body['normalizedState'], 'paused')
        self.assertIsNone(body['settings']['pausedAt'])
        self.assertEqual(body['settings']['indexSchedule'], DAILY)
        self.patch_json(source_url(source.pk), {'paused': False})
        source.refresh_from_db()
        self.assertEqual(source.index_schedule, DAILY)
        self.assertTrue(source.download_media)

    def test_unreadable_record_falls_back_to_the_default(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false', MEDIANEST_BRIDGE_SOURCE_DEFAULTS='{}')
        source = make_source()
        self.patch_json(source_url(source.pk), {'paused': True})
        (pause.record_dir() / f'{source.pk}.json').write_text('{not json', encoding='utf-8')
        self.patch_json(source_url(source.pk), {'paused': False})
        source.refresh_from_db()
        self.assertEqual(source.index_schedule, DAILY)

    def test_schedule_while_paused_sets_what_resume_brings_back(self):
        self.writable()
        source = make_source()
        self.patch_json(source_url(source.pk), {'paused': True})
        body = json.loads(self.patch_json(source_url(source.pk), {'indexSchedule': WEEKLY}).content)
        self.assertEqual(body['normalizedState'], 'paused')
        self.assertEqual(body['settings']['indexSchedule'], WEEKLY)
        source.refresh_from_db()
        self.assertEqual(source.index_schedule, NEVER)
        self.patch_json(source_url(source.pk), {'paused': False})
        source.refresh_from_db()
        self.assertEqual(source.index_schedule, WEEKLY)

    def test_resume_with_a_schedule_uses_it(self):
        self.writable()
        source = make_source()
        self.patch_json(source_url(source.pk), {'paused': True})
        self.patch_json(source_url(source.pk), {'paused': False, 'indexSchedule': DAILY})
        source.refresh_from_db()
        self.assertEqual(source.index_schedule, DAILY)

    def test_pause_with_a_schedule_records_it(self):
        self.writable()
        source = make_source()
        body = json.loads(self.patch_json(
            source_url(source.pk), {'paused': True, 'indexSchedule': WEEKLY},
        ).content)
        self.assertEqual(body['settings']['indexSchedule'], WEEKLY)

    def test_schedule_on_an_active_source_changes_it(self):
        self.writable()
        source = make_source()
        body = json.loads(self.patch_json(source_url(source.pk), {'indexSchedule': DAILY}).content)
        self.assertEqual(body['settings']['indexSchedule'], DAILY)
        source.refresh_from_db()
        self.assertEqual(source.index_schedule, DAILY)

    def test_resume_of_an_active_source_changes_nothing(self):
        self.writable()
        source = make_source()
        response = self.patch_json(source_url(source.pk), {'paused': False})
        self.assertEqual(response.status_code, 200)
        source.refresh_from_db()
        self.assertEqual(source.index_schedule, HOURLY)

    def test_bad_requests_change_nothing(self):
        self.writable()
        source = make_source()
        for body in (
            {}, {'downloadCap': '1y'}, {'paused': 'yes'}, {'paused': 1},
            {'indexSchedule': 0}, {'indexSchedule': True}, {'indexSchedule': '3600'},
            {'indexSchedule': 1234}, [], {'paused': True, 'extra': 1},
        ):
            response = self.patch_json(source_url(source.pk), body)
            self.assertEqual(response.status_code, 400, body)
            self.assertEqual(json.loads(response.content)['code'], 'SOURCE_INVALID')
        source.refresh_from_db()
        self.assertEqual(source.index_schedule, HOURLY)
        self.assertTrue(source.download_media)

    def test_video_list_cannot_be_paused(self):
        self.writable()
        source = make_source(
            source_type=Val(YouTube_SourceType.PLAYLIST),
            key='medianest-videolist-cooking', name='Cooking', directory='acq-src-cooking',
            index_videos=False, index_streams=False, index_schedule=NEVER,
        )
        body = json.loads(self.client.get(source_url(source.pk), **self.auth_header()).content)
        self.assertEqual(body['normalizedState'], 'active')
        self.assertFalse(body['settings']['paused'])
        self.assertIsNone(body['settings']['indexSchedule'])
        response = self.patch_json(source_url(source.pk), {'paused': True})
        self.assertEqual(response.status_code, 400)

    def test_unknown_source_is_not_found(self):
        self.writable()
        for source_uuid in (uuid.uuid4(), 'not-a-uuid'):
            response = self.patch_json(source_url(source_uuid), {'paused': True})
            self.assertEqual(response.status_code, 404)
            self.assertEqual(json.loads(response.content)['code'], 'SOURCE_NOT_FOUND')

    def test_read_only_bridge_refuses(self):
        self.enable_bridge()
        source = make_source()
        response = self.patch_json(source_url(source.pk), {'paused': True})
        self.assertEqual(response.status_code, 403)
        source.refresh_from_db()
        self.assertEqual(source.index_schedule, HOURLY)

    def test_a_source_turned_all_off_in_tubesync_stays_disabled(self):
        self.enable_bridge()
        source = make_source(
            index_schedule=NEVER, download_media=False,
            index_videos=False, index_streams=False,
        )
        body = json.loads(self.client.get(source_url(source.pk), **self.auth_header()).content)
        self.assertEqual(body['normalizedState'], 'disabled')
        self.assertFalse(body['settings']['paused'])


class PauseWhileDeletingTestCase(ControlsTestMixin, BridgeTransactionTestCase):

    def test_a_source_being_deleted_is_a_conflict(self):
        self.writable()
        with temp_download_root():
            source = make_source()
            with patch.object(TaskHistory, 'schedule'):
                self.client.delete(source_url(source.pk), **self.auth_header())
            response = self.patch_json(source_url(source.pk), {'paused': True})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(json.loads(response.content)['code'], 'SOURCE_DELETING')
            hidden = Source.objects.get(key=KEY)
            response = self.patch_json(source_url(hidden.pk), {'paused': True})
            self.assertEqual(response.status_code, 404)
            hidden.refresh_from_db()
            self.assertEqual(hidden.index_schedule, NEVER)


def make_failed_task(media, last_error='ERROR: HTTP Error 403: Forbidden', **overrides):
    failed_at = timezone.now()
    fields = dict(
        start_at=failed_at - timezone.timedelta(seconds=30),
        end_at=failed_at, scheduled_at=failed_at, failed_at=failed_at,
        last_error=last_error,
    )
    fields.update(overrides)
    return make_download_task(media, **fields)


def has_metadata(value):
    return patch.object(Media, 'has_metadata', new_callable=PropertyMock, return_value=value)


class RetryMediaTestCase(ControlsTestMixin, BridgeTestCase):

    def setUp(self):
        super().setUp()
        self.source = make_source()
        self.media = make_media(self.source)

    def scheduled(self, schedule_mock):
        return [getattr(call.args[0], 'name', None) for call in schedule_mock.call_args_list]

    def test_failed_video_with_metadata_queues_a_manual_download(self):
        self.writable()
        make_failed_task(self.media)
        with has_metadata(True), patch.object(Media, 'save_to_metadata'), \
                patch.object(TaskHistory, 'schedule') as schedule:
            response = self.post(media_url(self.source.pk, self.media.pk, 'retry'))
        self.assertEqual(response.status_code, 202)
        assert_matches_schema(self, json.loads(response.content), 'MediaItem')
        names = self.scheduled(schedule)
        self.assertIn(refresh_formats.name, names)
        self.assertIn(download_media_file.name, names)
        manual = [
            call for call in schedule.call_args_list
            if call.args[0] is download_media_file and call.kwargs.get('override')
        ]
        self.assertEqual(len(manual), 1)

    def test_retry_of_a_queued_video_changes_nothing(self):
        # CodeRabbit review on #86: the contract says a video already
        # queued is left as it is.
        self.writable()
        make_download_task(
            self.media, start_at=None,
            scheduled_at=timezone.now() + timezone.timedelta(minutes=5),
        )
        with patch.object(TaskHistory, 'schedule') as schedule:
            response = self.post(media_url(self.source.pk, self.media.pk, 'retry'))
        self.assertEqual(response.status_code, 202)
        schedule.assert_not_called()

    def test_video_without_metadata_fetches_it_first(self):
        self.writable()
        with patch.object(TaskHistory, 'schedule') as schedule:
            response = self.post(media_url(self.source.pk, self.media.pk, 'retry'))
        self.assertEqual(response.status_code, 202)
        calls = [c for c in schedule.call_args_list if c.args[0] is download_media_metadata]
        self.assertTrue(calls)
        self.assertTrue(any(c.kwargs.get('manual') for c in calls))
        self.assertTrue(any(c.kwargs.get('download_after') for c in calls))

    def test_retry_unskips_a_skipped_video(self):
        self.writable()
        Media.objects.filter(pk=self.media.pk).update(skip=True, manual_skip=True)
        with patch.object(TaskHistory, 'schedule'):
            response = self.post(media_url(self.source.pk, self.media.pk, 'retry'))
        self.assertEqual(response.status_code, 202)
        self.media.refresh_from_db()
        self.assertFalse(self.media.manual_skip)

    def test_downloaded_video_is_refused(self):
        self.writable()
        Media.objects.filter(pk=self.media.pk).update(downloaded=True)
        with patch.object(TaskHistory, 'schedule') as schedule:
            response = self.post(media_url(self.source.pk, self.media.pk, 'retry'))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.content)['code'], 'MEDIA_DOWNLOADED')
        schedule.assert_not_called()

    def test_paused_source_is_refused(self):
        self.writable()
        Source.objects.filter(pk=self.source.pk).update(index_schedule=NEVER, download_media=False)
        with patch.object(TaskHistory, 'schedule') as schedule:
            response = self.post(media_url(self.source.pk, self.media.pk, 'retry'))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.content)['code'], 'SOURCE_PAUSED')
        schedule.assert_not_called()

    def test_a_running_download_is_left_alone(self):
        self.writable()
        make_download_task(self.media)  # the running() shape
        with patch.object(TaskHistory, 'schedule') as schedule:
            response = self.post(media_url(self.source.pk, self.media.pk, 'retry'))
        self.assertEqual(response.status_code, 202)
        self.assertEqual(json.loads(response.content)['normalizedState'], 'downloading')
        schedule.assert_not_called()

    def test_media_of_another_source_is_not_found(self):
        self.writable()
        other = make_source(key='UCzzzzzzzzzzzzzzzzzzzzzz', name='other', directory='other')
        for media_id in (self.media.pk, uuid.uuid4(), 'nope'):
            response = self.post(media_url(other.pk, media_id, 'retry'))
            self.assertEqual(response.status_code, 404)
            self.assertEqual(json.loads(response.content)['code'], 'MEDIA_NOT_FOUND')

    def test_read_only_bridge_refuses(self):
        self.enable_bridge()
        response = self.post(media_url(self.source.pk, self.media.pk, 'retry'))
        self.assertEqual(response.status_code, 403)


class SkipMediaTestCase(ControlsTestMixin, BridgeTestCase):

    def setUp(self):
        super().setUp()
        self.source = make_source()
        self.media = make_media(self.source)

    def test_skip_marks_the_video_and_revokes_queued_downloads(self):
        self.writable()
        future = timezone.now() + timezone.timedelta(minutes=5)
        queued = make_download_task(
            self.media, start_at=None, scheduled_at=future,
            task_params=[[str(self.media.pk)], repr({'override': True})],
        )
        failed = make_failed_task(
            self.media, last_error='ERROR: Private video',
            task_params=[[str(self.media.pk)], repr({'override': True})],
        )
        response = self.post(media_url(self.source.pk, self.media.pk, 'skip'))
        self.assertEqual(response.status_code, 202)
        body = json.loads(response.content)
        assert_matches_schema(self, body, 'MediaItem')
        self.assertEqual(body['normalizedState'], 'skipped')
        self.assertIsNone(body['error'])
        self.assertIsNone(body['failureKind'])
        self.media.refresh_from_db()
        self.assertTrue(self.media.skip)
        self.assertTrue(self.media.manual_skip)
        for task in (queued, failed):
            task.refresh_from_db()
            self.assertTrue(task.verbose_name.startswith('[revoked] '))

    def test_skip_revokes_a_failed_download_whose_retry_is_due(self):
        # Codex review on #86: a retry that is due but still waiting for a
        # worker has start_at set and scheduled_at in the past; huey must
        # still be told to drop it, or its override=True downloads anyway.
        self.writable()
        due = make_failed_task(
            self.media,
            scheduled_at=timezone.now() - timezone.timedelta(seconds=5),
            task_params=[[str(self.media.pk)], repr({'override': True})],
        )
        from .. import views_controls
        with patch.object(
            views_controls, '_revoke_pending_index_task',
            wraps=views_controls._revoke_pending_index_task,
        ) as revoke:
            response = self.post(media_url(self.source.pk, self.media.pk, 'skip'))
        self.assertEqual(response.status_code, 202)
        self.assertEqual([call.args[0].pk for call in revoke.call_args_list], [due.pk])
        due.refresh_from_db()
        self.assertTrue(due.verbose_name.startswith('[revoked] '))

    def test_skip_while_metadata_is_being_fetched_is_busy(self):
        # CodeRabbit review on #86: the fetch saves the whole row when it
        # finishes, which would undo the skip.
        self.writable()
        now = timezone.now()
        make_download_task(
            self.media, name='sync.tasks.download_media_metadata',
            start_at=now, end_at=now, scheduled_at=now - timezone.timedelta(seconds=5),
        )
        response = self.post(media_url(self.source.pk, self.media.pk, 'skip'))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.content)['code'], 'MEDIA_BUSY')
        self.media.refresh_from_db()
        self.assertFalse(self.media.manual_skip)

    def test_skip_survives_a_later_save(self):
        self.writable()
        self.post(media_url(self.source.pk, self.media.pk, 'skip'))
        media = Media.objects.get(pk=self.media.pk)
        media.save()
        media.refresh_from_db()
        self.assertTrue(media.skip)

    def test_skip_twice_is_fine(self):
        self.writable()
        self.post(media_url(self.source.pk, self.media.pk, 'skip'))
        response = self.post(media_url(self.source.pk, self.media.pk, 'skip'))
        self.assertEqual(response.status_code, 202)

    def test_retry_undoes_skip(self):
        self.writable()
        self.post(media_url(self.source.pk, self.media.pk, 'skip'))
        with patch.object(TaskHistory, 'schedule'):
            self.post(media_url(self.source.pk, self.media.pk, 'retry'))
        self.media.refresh_from_db()
        self.assertFalse(self.media.manual_skip)

    def test_downloaded_video_is_refused(self):
        self.writable()
        Media.objects.filter(pk=self.media.pk).update(downloaded=True)
        response = self.post(media_url(self.source.pk, self.media.pk, 'skip'))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.content)['code'], 'MEDIA_DOWNLOADED')
        self.media.refresh_from_db()
        self.assertFalse(self.media.manual_skip)

    def test_downloading_video_is_busy(self):
        self.writable()
        make_download_task(self.media)
        response = self.post(media_url(self.source.pk, self.media.pk, 'skip'))
        self.assertEqual(response.status_code, 409)
        body = json.loads(response.content)
        self.assertEqual(body['code'], 'MEDIA_BUSY')
        self.assertTrue(body['retryable'])

    def test_a_paused_source_can_still_skip(self):
        self.writable()
        self.patch_json(source_url(self.source.pk), {'paused': True})
        response = self.post(media_url(self.source.pk, self.media.pk, 'skip'))
        self.assertEqual(response.status_code, 202)


class FailureKindMappingTestCase(BridgeTestCase):

    def test_media_item_carries_the_kind_with_the_error(self):
        source = make_source()
        media = make_media(source)
        make_failed_task(
            media,
            last_error=(
                'Traceback (most recent call last):\n'
                '  ...\n'
                'yt_dlp.utils.DownloadError: ERROR: [youtube] dQw4w9WgXcQ: Private video. '
                'Sign in if you\'ve been granted access to this video'
            ),
        )
        body = mapping.serialize_media(media)
        self.assertEqual(body['normalizedState'], 'failed')
        self.assertIsNotNone(body['error'])
        self.assertEqual(body['failureKind'], 'private')

    def test_no_error_no_kind(self):
        source = make_source()
        body = mapping.serialize_media(make_media(source))
        self.assertIsNone(body['error'])
        self.assertIsNone(body['failureKind'])


class FailureKindClassifyTestCase(SimpleTestCase):

    CASES = {
        'ERROR: [youtube] x: Video unavailable. This video has been removed by the uploader': 'unavailable',
        'ERROR: [youtube] x: This video is no longer available because the YouTube account associated with this video has been terminated.': 'unavailable',
        'ERROR: [youtube] x: Private video. Sign in if you\'ve been granted access': 'private',
        'ERROR: [youtube] x: Join this channel to get access to members-only content like this video': 'members_only',
        'ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate for some users.': 'age_restricted',
        'ERROR: [youtube] x: The uploader has not made this video available in your country': 'geo_blocked',
        'ERROR: [youtube] x: Premieres in 3 hours': 'upcoming',
        'ERROR: [youtube] x: This live event will begin in a few moments.': 'upcoming',
        "ERROR: [youtube] x: Sign in to confirm you're not a bot.": 'temporary',
        'ERROR: unable to download video data: HTTP Error 403: Forbidden': 'temporary',
        'OSError: [Errno 101] Network is unreachable': 'temporary',
        'OSError: [Errno 28] No space left on device': 'disk_full',
        'NoFormatException: Cannot download, media has no valid format available': 'unknown',
        '': 'unknown',
        None: 'unknown',
    }

    def test_known_phrases(self):
        for text, expected in self.CASES.items():
            with self.subTest(text=text):
                self.assertEqual(failure_kind.classify(text), expected)

    def test_every_kind_is_in_the_contract(self):
        import json as _json
        from pathlib import Path as _Path
        fixtures = _json.loads(
            (_Path(__file__).resolve().parent.parent / 'contract' / 'contract_fixtures.json')
            .read_text(encoding='utf-8'),
        )
        enum = fixtures['schemas']['MediaItem']['properties']['failureKind']['enum']
        for kind in failure_kind.KNOWN_KINDS:
            self.assertIn(kind, enum)
