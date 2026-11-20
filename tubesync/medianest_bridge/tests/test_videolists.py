'''
    Contract 1.5.0 (MED-730, MediaNest ADR-0030): video lists.
    POST /videolists, POST /sources/{uuid}/videos and
    DELETE /sources/{uuid}/videos/{videoId}.
'''
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.utils import timezone
from huey.exceptions import TaskLockedException

from common.models import TaskHistory
from sync.choices import IndexSchedule, Val, YouTube_SourceType
from sync.models import Media, Source
from sync.models._migrations import media_file_storage
from sync.tasks import download_media_metadata

from .. import videolist
from .base import BridgeTestCase
from .test_delete_sources import temp_download_root
from .test_endpoints import assert_matches_schema

API = '/api/medianest/v1'
KEY = 'medianest-videolist-cooking'
NAME = 'Cooking: Single videos'
FOLDER = f'acq-src-{KEY}'
VIDEO = 'dQw4w9WgXcQ'


def videos_url(source_uuid):
    return f'{API}/sources/{source_uuid}/videos'


def video_url(source_uuid, video_id):
    return f'{API}/sources/{source_uuid}/videos/{video_id}'


def make_videolist(**overrides):
    fields = dict(
        source_type=Val(YouTube_SourceType.PLAYLIST),
        key=KEY, name=NAME, directory=FOLDER,
        index_videos=False, index_streams=False,
        index_schedule=Val(IndexSchedule.NEVER),
    )
    fields.update(overrides)
    return Source.objects.create(**fields)


class VideoListTestCase(BridgeTestCase):

    def setUp(self):
        super().setUp()
        # Keep TubeSync's own task scheduling out of these tests: the
        # assertions read what would have been scheduled instead.
        self.schedule = patch.object(TaskHistory, 'schedule').start()
        self.image = patch('sync.tasks.download_media_image').start()
        self.addCleanup(patch.stopall)

    def post_json(self, url, body):
        return self.client.post(
            url, data=json.dumps(body), content_type='application/json',
            **self.auth_header(),
        )

    def create(self, **overrides):
        body = {'canonicalKey': KEY, 'name': NAME, 'directory': FOLDER}
        body.update(overrides)
        return self.post_json(f'{API}/videolists', body)


class CreateVideoListTestCase(VideoListTestCase):

    def test_creates_a_non_indexing_downloading_source(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        response = self.create()
        self.assertEqual(response.status_code, 201, response.content)
        body = json.loads(response.content)
        assert_matches_schema(self, body, 'Source')
        self.assertEqual(body['sourceType'], 'videolist')
        self.assertIsNone(body['canonicalUrl'])
        self.assertEqual(body['normalizedState'], 'active')
        self.assertEqual(body['name'], NAME)
        source = Source.objects.get(key=KEY)
        self.assertEqual(source.source_type, Val(YouTube_SourceType.PLAYLIST))
        self.assertFalse(source.index_videos)
        self.assertFalse(source.index_streams)
        self.assertEqual(source.index_schedule, Val(IndexSchedule.NEVER))
        self.assertFalse(source.is_active)
        self.assertTrue(source.download_media)
        self.assertFalse(source.copy_channel_images)
        self.assertFalse(source.delete_removed_media)
        self.assertFalse(source.delete_files_on_disk)
        self.assertEqual(source.download_cap, 0)
        self.assertTrue(videolist.is_videolist(source))

    def test_forced_fields_win_over_the_playlist_defaults(self):
        self.enable_bridge(
            MEDIANEST_BRIDGE_READ_ONLY='false',
            MEDIANEST_BRIDGE_SOURCE_DEFAULTS=json.dumps({
                '*': {
                    'copy_channel_images': True,
                    'delete_files_on_disk': True,
                    'index_videos': True,
                    'write_nfo': True,
                },
            }),
        )
        response = self.create()
        self.assertEqual(response.status_code, 201, response.content)
        source = Source.objects.get(key=KEY)
        self.assertFalse(source.copy_channel_images)
        self.assertFalse(source.delete_files_on_disk)
        self.assertFalse(source.index_videos)
        # Overlay fields that are not forced still apply.
        self.assertTrue(source.write_nfo)

    def test_existing_key_is_a_conflict_to_adopt(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        existing = make_videolist()
        response = self.create()
        self.assertEqual(response.status_code, 409)
        body = json.loads(response.content)
        self.assertEqual(body['code'], 'SOURCE_CONFLICT')
        self.assertEqual(body['existingSourceUuid'], str(existing.pk))

    def test_name_used_by_another_source_is_a_namespace_conflict(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        make_videolist(key='medianest-videolist-other', directory='elsewhere')
        response = self.create()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)['code'], 'SOURCE_NAMESPACE_CONFLICT')

    def test_key_without_the_reserved_prefix_is_refused(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        for key in ('PLabcdefghijk', 'medianest-videolist-', 'medianest-videolist-UPPER'):
            response = self.create(canonicalKey=key)
            self.assertEqual(response.status_code, 400, key)
            self.assertEqual(json.loads(response.content)['code'], 'SOURCE_INVALID')
        self.assertFalse(Source.objects.exists())

    def test_unknown_field_is_refused(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        response = self.create(sourceType='playlist')
        self.assertEqual(response.status_code, 400)

    def test_read_only_bridge_refuses(self):
        self.enable_bridge()
        response = self.create()
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Source.objects.exists())

    def test_post_sources_refuses_the_reserved_prefix(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        response = self.post_json(f'{API}/sources', {
            'sourceType': 'playlist',
            'canonicalKey': KEY,
            'canonicalUrl': f'https://www.youtube.com/playlist?list={KEY}',
            'name': FOLDER,
            'directory': FOLDER,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('reserved', json.loads(response.content)['detail'])
        self.assertFalse(Source.objects.exists())

    def test_get_source_reports_a_video_list(self):
        self.enable_bridge()
        source = make_videolist()
        response = self.client.get(f'{API}/sources/{source.pk}', **self.auth_header())
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        assert_matches_schema(self, body, 'Source')
        self.assertEqual(body['sourceType'], 'videolist')
        self.assertEqual(body['normalizedState'], 'active')

    def test_an_ordinary_playlist_is_not_a_video_list(self):
        source = make_videolist(key='PLabcdefghijklmnop')
        self.assertFalse(videolist.is_videolist(source))


class AddVideoTestCase(VideoListTestCase):

    def metadata_schedules(self):
        return [
            call for call in self.schedule.call_args_list
            if call.args and call.args[0] is download_media_metadata
        ]

    def test_adds_the_video_and_schedules_its_fetches(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        source = make_videolist()
        response = self.post_json(videos_url(source.pk), {'videoId': VIDEO})
        self.assertEqual(response.status_code, 201, response.content)
        body = json.loads(response.content)
        assert_matches_schema(self, body, 'MediaItem')
        self.assertEqual(body['youtubeKey'], VIDEO)
        self.assertEqual(body['sourceId'], str(source.pk))
        media = Media.objects.get(source=source, key=VIDEO)
        self.assertEqual(body['id'], str(media.pk))
        self.assertEqual(len(self.metadata_schedules()), 1)
        self.assertEqual(self.metadata_schedules()[0].args[1], str(media.pk))
        self.assertEqual(self.image.call_count, 3)

    def test_adding_twice_is_idempotent(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        source = make_videolist()
        first = self.post_json(videos_url(source.pk), {'videoId': VIDEO})
        second = self.post_json(videos_url(source.pk), {'videoId': VIDEO})
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(json.loads(first.content)['id'], json.loads(second.content)['id'])
        self.assertEqual(Media.objects.filter(source=source).count(), 1)
        self.assertEqual(len(self.metadata_schedules()), 1)

    def test_metadata_unskips_the_video(self):
        '''
            The row starts skipped (no published date yet). The save after
            its metadata arrives is what un-skips it, as for an indexed
            video; this checks that path runs for a video-list row too.
        '''
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        source = make_videolist()
        self.post_json(videos_url(source.pk), {'videoId': VIDEO})
        media = Media.objects.get(source=source, key=VIDEO)
        self.assertTrue(media.skip)
        self.assertFalse(media.manual_skip)
        media.published = timezone.now()
        media.save()
        media.refresh_from_db()
        self.assertFalse(media.skip)

    def test_only_video_lists_take_videos(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        source = make_videolist(key='PLabcdefghijklmnop', index_videos=True)
        response = self.post_json(videos_url(source.pk), {'videoId': VIDEO})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Media.objects.exists())

    def test_malformed_video_id_is_refused(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        source = make_videolist()
        for value in ('short', 'dQw4w9WgXcQx', 'dQw4w9WgX/Q', 12345678901, None):
            response = self.post_json(videos_url(source.pk), {'videoId': value})
            self.assertEqual(response.status_code, 400, value)
        response = self.post_json(videos_url(source.pk), {'videoId': VIDEO, 'extra': 1})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Media.objects.exists())

    def test_unknown_source_is_not_found(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        response = self.post_json(
            videos_url('00000000-0000-4000-8000-000000000000'), {'videoId': VIDEO},
        )
        self.assertEqual(response.status_code, 404)

    def test_read_only_bridge_refuses(self):
        self.enable_bridge()
        source = make_videolist()
        response = self.post_json(videos_url(source.pk), {'videoId': VIDEO})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Media.objects.exists())


class RemoveVideoTestCase(VideoListTestCase):

    def downloaded(self, source, key=VIDEO):
        folder = Path(source.directory_path)
        folder.mkdir(parents=True, exist_ok=True)
        video = folder / f'{key}.mkv'
        video.write_bytes(b'video')
        video.with_suffix('.nfo').write_text('<episodedetails/>')
        media = Media.objects.create(source=source, key=key, title=key, published=timezone.now())
        Media.objects.filter(pk=media.pk).update(
            downloaded=True,
            media_file=str(video.relative_to(media_file_storage.location)),
        )
        return video

    def test_keeps_files_by_default(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root():
            source = make_videolist()
            video = self.downloaded(source)
            response = self.client.delete(video_url(source.pk, VIDEO), **self.auth_header())
            self.assertEqual(response.status_code, 204)
            self.assertFalse(Media.objects.exists())
            self.assertTrue(video.exists())
            self.assertTrue(video.with_suffix('.nfo').exists())
            source.refresh_from_db()
            self.assertFalse(source.delete_files_on_disk)

    def test_delete_files_removes_the_file_and_sidecars(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root():
            source = make_videolist()
            video = self.downloaded(source)
            other = self.downloaded(source, key='aaaaaaaaaaa')
            response = self.client.delete(
                video_url(source.pk, VIDEO) + '?deleteFiles=true', **self.auth_header(),
            )
            self.assertEqual(response.status_code, 204)
            self.assertFalse(video.exists())
            self.assertFalse(video.with_suffix('.nfo').exists())
            self.assertTrue(other.exists())
            self.assertEqual(Media.objects.count(), 1)
            # The flag was set on the deleted row's in-memory source only.
            source.refresh_from_db()
            self.assertFalse(source.delete_files_on_disk)

    def test_a_video_that_is_downloading_is_refused_and_kept(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        source = make_videolist()
        Media.objects.create(source=source, key=VIDEO, title='x')
        lock = MagicMock()
        lock.__enter__.side_effect = TaskLockedException('media lock held')
        with patch('medianest_bridge.videolist.huey_lock_task', return_value=lock) as taken:
            response = self.client.delete(
                video_url(source.pk, VIDEO) + '?deleteFiles=true', **self.auth_header(),
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'MEDIA_BUSY')
        self.assertTrue(response.json()['retryable'])
        self.assertTrue(Media.objects.filter(key=VIDEO).exists())
        media = Media.objects.get(key=VIDEO)
        taken.assert_called_once()
        self.assertEqual(taken.call_args.args[0], f'media:{media.uuid}')

    def test_absent_video_is_still_204(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        source = make_videolist()
        response = self.client.delete(video_url(source.pk, VIDEO), **self.auth_header())
        self.assertEqual(response.status_code, 204)

    def test_bad_query_or_id_is_refused(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        source = make_videolist()
        response = self.client.delete(
            video_url(source.pk, VIDEO) + '?deleteFiles=maybe', **self.auth_header(),
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.delete(video_url(source.pk, 'nope'), **self.auth_header())
        self.assertEqual(response.status_code, 400)

    def test_only_video_lists_lose_videos(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        source = make_videolist(key='PLabcdefghijklmnop')
        Media.objects.create(source=source, key=VIDEO, title='x')
        response = self.client.delete(video_url(source.pk, VIDEO), **self.auth_header())
        self.assertEqual(response.status_code, 400)
        self.assertTrue(Media.objects.exists())

    def test_read_only_bridge_refuses(self):
        self.enable_bridge()
        source = make_videolist()
        response = self.client.delete(video_url(source.pk, VIDEO), **self.auth_header())
        self.assertEqual(response.status_code, 403)

    def test_a_removed_video_can_be_added_again(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        source = make_videolist()
        self.post_json(videos_url(source.pk), {'videoId': VIDEO})
        self.client.delete(video_url(source.pk, VIDEO), **self.auth_header())
        self.assertFalse(Media.objects.exists())
        response = self.post_json(videos_url(source.pk), {'videoId': VIDEO})
        self.assertEqual(response.status_code, 201)

    def test_a_video_skipped_by_hand_is_revived(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        source = make_videolist()
        media = Media.objects.create(
            source=source, key=VIDEO, title='x', skip=True, manual_skip=True,
        )
        response = self.post_json(videos_url(source.pk), {'videoId': VIDEO})
        self.assertEqual(response.status_code, 201)
        media.refresh_from_db()
        self.assertFalse(media.manual_skip)
