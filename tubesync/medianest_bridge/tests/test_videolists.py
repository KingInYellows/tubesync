'''
    Contract 1.5.0 (MED-730, MediaNest ADR-0030): video lists.
    POST /videolists, POST /sources/{uuid}/videos and
    DELETE /sources/{uuid}/videos/{videoId}.
'''
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.db import DatabaseError, transaction
from django.utils import timezone
from django.conf import settings
from huey.exceptions import CancelExecution, TaskLockedException
from PIL import Image

from common.models import TaskHistory
from sync import tasks as sync_tasks
from sync.choices import IndexSchedule, TaskQueue, Val, YouTube_SourceType
from sync.models import Media, Source
from sync.models._migrations import media_file_storage
from sync.tasks import download_media_image as real_download_media_image
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
        # Thumbnails are queued once. A row still without metadata gets its
        # metadata task asked for again (repairs a lost one); TaskHistory's
        # remove_duplicates keeps a queued one single.
        self.assertEqual(self.image.call_count, 3)
        self.assertEqual(len(self.metadata_schedules()), 2)
        for call in self.metadata_schedules():
            self.assertTrue(call.kwargs['remove_duplicates'])

    def test_adding_a_video_with_metadata_again_schedules_nothing(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        source = make_videolist()
        self.post_json(videos_url(source.pk), {'videoId': VIDEO})
        self.schedule.reset_mock()
        with patch.object(Media, 'has_metadata', new=True):
            response = self.post_json(videos_url(source.pk), {'videoId': VIDEO})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.metadata_schedules(), [])

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
        for value in ('short', 'dQw4w9WgXcQx', 'dQw4w9WgX/Q', VIDEO + '\n', 12345678901, None):
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

    def test_a_task_holding_the_removed_video_cannot_bring_it_back(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        source = make_videolist()
        Media.objects.create(source=source, key=VIDEO, title='x')
        # A thumbnail or metadata task read the row before the removal.
        stale = Media.objects.get(key=VIDEO)
        response = self.client.delete(video_url(source.pk, VIDEO), **self.auth_header())
        self.assertEqual(response.status_code, 204)
        with self.assertRaises(DatabaseError), transaction.atomic():
            stale.save()
        self.assertFalse(Media.objects.filter(key=VIDEO).exists())

    def test_a_download_that_finished_just_before_still_loses_its_files(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root():
            source = make_videolist()
            Media.objects.create(source=source, key=VIDEO, title=VIDEO, published=timezone.now())
            folder = Path(source.directory_path)
            folder.mkdir(parents=True, exist_ok=True)
            video = folder / f'{VIDEO}.mkv'
            real_lock = videolist.huey_lock_task

            def finish_download_then_lock(*args, **kwargs):
                # The download completes after remove_video's first read.
                video.write_bytes(b'video')
                Media.objects.filter(key=VIDEO).update(
                    downloaded=True,
                    media_file=str(video.relative_to(media_file_storage.location)),
                )
                return real_lock(*args, **kwargs)

            with patch('medianest_bridge.videolist.huey_lock_task', side_effect=finish_download_then_lock):
                response = self.client.delete(
                    video_url(source.pk, VIDEO) + '?deleteFiles=true', **self.auth_header(),
                )
            self.assertEqual(response.status_code, 204)
            self.assertFalse(video.exists())
            self.assertFalse(Media.objects.exists())

    def test_a_thumbnail_fetched_for_a_removed_video_is_not_kept(self):
        '''
            The thumbnail task read the row, then the video was removed
            while it fetched the image: it keeps neither the row nor the
            cached image.
        '''
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root():
            source = make_videolist()
            media = Media.objects.create(source=source, key=VIDEO, title=VIDEO)
            # Not skipped, as after its metadata landed.
            Media.objects.filter(pk=media.pk).update(skip=False, manual_skip=False)

            removed = {}

            def removed_during_fetch(url):
                response = self.client.delete(video_url(source.pk, VIDEO), **self.auth_header())
                removed['status'] = response.status_code
                removed['row_left'] = Media.objects.filter(pk=media.pk).exists()
                # The task's save now fails the way Media.save() fails for
                # a deleted row (test_a_task_holding_the_removed_video_*
                # covers that part). Letting the real UPDATE run here
                # leaves the test's in-memory SQLite with a table lock
                # that breaks every later query.
                save_patch.start()
                return Image.new('RGB', (64, 36))

            save_patch = patch.object(
                Media, 'save', side_effect=DatabaseError('Forced update did not affect any rows.'),
            )
            self.addCleanup(patch.stopall)

            # The failed save marks the test transaction for rollback, so
            # the task runs in a savepoint and the row is checked above.
            with patch('sync.tasks.get_remote_image', side_effect=removed_during_fetch):
                with self.assertRaises(CancelExecution), transaction.atomic():
                    real_download_media_image.call_local(str(media.pk), 'https://i.ytimg.com/x.jpg')
            self.assertEqual(removed, {'status': 204, 'row_left': False})
            thumb = Path(settings.MEDIA_ROOT) / media.thumb.field.upload_to(media, 'thumb')
            self.assertFalse(thumb.exists())

    def test_a_thumbnail_copy_never_lands_beside_a_removed_video(self):
        '''
            The thumbnail task saved its image, then the video and its
            files were removed just before it copied the image next to
            the video: no thumbnail sidecar is left, and adding the video
            again works.
        '''
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root():
            source = make_videolist(copy_thumbnails=True)
            folder = Path(source.directory_path)
            folder.mkdir(parents=True, exist_ok=True)
            video = folder / f'{VIDEO}.mkv'
            video.write_bytes(b'video')
            media = Media.objects.create(source=source, key=VIDEO, title=VIDEO, published=timezone.now())
            Media.objects.filter(pk=media.pk).update(
                skip=False, manual_skip=False, downloaded=True,
                media_file=str(video.relative_to(media_file_storage.location)),
            )
            media.refresh_from_db()
            sidecar = media.thumbpath
            real_lock = sync_tasks.huey_lock_task
            removed = {}

            def remove_then_lock(*args, **kwargs):
                # The removal runs between the row check and the copy.
                response = self.client.delete(
                    video_url(source.pk, VIDEO) + '?deleteFiles=true', **self.auth_header(),
                )
                removed['status'] = response.status_code
                return real_lock(*args, **kwargs)

            with (
                patch('sync.tasks.get_remote_image', return_value=Image.new('RGB', (64, 36))),
                patch('sync.tasks.huey_lock_task', side_effect=remove_then_lock),
            ):
                real_download_media_image.call_local(str(media.pk), 'https://i.ytimg.com/x.jpg')
            self.assertEqual(removed, {'status': 204})
            self.assertFalse(video.exists())
            self.assertFalse(sidecar.exists())
            self.assertFalse(Media.objects.exists())

            response = self.post_json(videos_url(source.pk), {'videoId': VIDEO})
            self.assertEqual(response.status_code, 201)
            self.assertTrue(Media.objects.filter(key=VIDEO).exists())

    def test_a_thumbnail_copy_skips_media_a_removal_holds(self):
        '''A removal holding the media lock means no copy, and no error.'''
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root():
            source = make_videolist(copy_thumbnails=True)
            folder = Path(source.directory_path)
            folder.mkdir(parents=True, exist_ok=True)
            video = folder / f'{VIDEO}.mkv'
            video.write_bytes(b'video')
            media = Media.objects.create(source=source, key=VIDEO, title=VIDEO, published=timezone.now())
            Media.objects.filter(pk=media.pk).update(
                skip=False, manual_skip=False, downloaded=True,
                media_file=str(video.relative_to(media_file_storage.location)),
            )
            media.refresh_from_db()
            with (
                patch('sync.tasks.get_remote_image', return_value=Image.new('RGB', (64, 36))),
                videolist.huey_lock_task(f'media:{media.uuid}', queue=Val(TaskQueue.DB)),
            ):
                self.assertTrue(
                    real_download_media_image.call_local(str(media.pk), 'https://i.ytimg.com/x.jpg'),
                )
            self.assertFalse(media.thumbpath.exists())

    def test_a_thumbnail_save_that_fails_for_a_present_row_is_retried(self):
        '''A transient database error is not mistaken for a removed video.'''
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root():
            source = make_videolist()
            media = Media.objects.create(source=source, key=VIDEO, title=VIDEO)
            Media.objects.filter(pk=media.pk).update(skip=False, manual_skip=False)
            with (
                patch('sync.tasks.get_remote_image', return_value=Image.new('RGB', (64, 36))),
                patch.object(Media, 'save', side_effect=DatabaseError('database is locked')),
                self.assertRaises(DatabaseError),
            ):
                real_download_media_image.call_local(str(media.pk), 'https://i.ytimg.com/x.jpg')
            self.assertTrue(Media.objects.filter(pk=media.pk).exists())

    def test_a_video_added_again_gets_back_the_file_it_kept(self):
        '''
            Removed with its files kept, then added again: the new row
            takes the kept file, so a later removal can delete it.
        '''
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root():
            source = make_videolist()
            response = self.post_json(videos_url(source.pk), {'videoId': VIDEO})
            self.assertEqual(response.status_code, 201)
            media = Media.objects.get(key=VIDEO)
            video = media.filepath
            video.parent.mkdir(parents=True, exist_ok=True)
            video.write_bytes(b'video')
            # Any later save of the row (metadata landing, for one).
            media.save()

            media.refresh_from_db()
            self.assertTrue(media.downloaded)
            self.assertEqual(Path(media.media_file.path), video)
            response = self.client.delete(
                video_url(source.pk, VIDEO) + '?deleteFiles=true', **self.auth_header(),
            )
            self.assertEqual(response.status_code, 204)
            self.assertFalse(video.exists())

    def test_a_kept_file_is_not_taken_by_an_ordinary_source(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root():
            source = make_videolist(key='PLordinary0000000000000000000000000')
            media = Media.objects.create(source=source, key=VIDEO, title=VIDEO)
            media.filepath.parent.mkdir(parents=True, exist_ok=True)
            media.filepath.write_bytes(b'video')
            media.save()
            media.refresh_from_db()
            self.assertFalse(media.downloaded)

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
        response = self.client.delete(video_url(source.pk, VIDEO + '%0A'), **self.auth_header())
        self.assertIn(response.status_code, (400, 404))

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
