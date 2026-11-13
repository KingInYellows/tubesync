'''
    Contract 1.4.0 (MED-657, MediaNest ADR-0022):
    DELETE /sources/{sourceUuid}?deleteFiles=<bool> and
    DELETE /folders/{sourceKey}.

    The end-to-end cases run TubeSync's own delete_all_media_for_source task
    in-process (call_local) after the request, so they use a
    TransactionTestCase: the task opens durable transactions, which a
    TestCase's wrapping transaction forbids.
'''
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone

from common.models import TaskHistory
from sync.choices import Val, YouTube_SourceType
from sync.models import Media, Source
from sync.models._migrations import media_file_storage
from sync.source_delete import prepare_source_for_deletion
from sync.tasks import delete_all_media_for_source

from .. import deletion
from .base import BridgeTestCase, BridgeTransactionTestCase
from .test_endpoints import assert_matches_schema

KEY = 'UCabcdefghijklmnopqrstuv'
FOLDER = f'acq-src-{KEY}'
SOURCES_URL = '/api/medianest/v1/sources'
CAPS_URL = '/api/medianest/v1/capabilities'


def source_url(source_uuid):
    return f'{SOURCES_URL}/{source_uuid}'


def folder_url(key):
    return f'/api/medianest/v1/folders/{key}'


@contextmanager
def temp_download_root():
    with tempfile.TemporaryDirectory() as tmp_dir:
        with (
            override_settings(DOWNLOAD_ROOT=tmp_dir),
            patch.object(media_file_storage, 'location', tmp_dir),
        ):
            yield Path(tmp_dir)


def run_background_inline():
    '''Folder removal normally runs in a thread; run it inline in tests.'''
    return patch.object(
        deletion, '_remove_in_background',
        side_effect=lambda paths: [deletion._remove_tree(p) for p in paths],
    )


def make_source(**overrides):
    defaults = dict(
        source_type=Val(YouTube_SourceType.CHANNEL_ID),
        key=KEY,
        name=FOLDER,
        directory=FOLDER,
    )
    defaults.update(overrides)
    return Source.objects.create(**defaults)


def make_downloaded_media(source, name='video1'):
    '''A downloaded Media row with a real file (plus an .nfo) on disk.'''
    folder = Path(source.directory_path)
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / f'{name}.mkv'
    video.write_bytes(b'video')
    (folder / f'{name}.nfo').write_text('<episodedetails/>')
    media = Media.objects.create(
        source=source, key=name, title=name, published=timezone.now(),
    )
    Media.objects.filter(pk=media.pk).update(
        downloaded=True,
        media_file=str(video.relative_to(media_file_storage.location)),
    )
    return video


def scheduled_deletes(schedule_mock):
    return sum(
        1 for call in schedule_mock.call_args_list
        if call.args and call.args[0] is delete_all_media_for_source
    )


def run_delete_task(media_source):
    '''What TubeSync's huey worker runs after the delete commits.'''
    delete_all_media_for_source.call_local(
        str(media_source.pk), str(media_source.name), str(media_source.directory_path),
    )


class DeleteSourceEndToEndTestCase(BridgeTransactionTestCase):

    def delete(self, source_uuid, delete_files=None, **extra):
        url = source_url(source_uuid)
        if delete_files is not None:
            url += f'?deleteFiles={delete_files}'
        with patch.object(TaskHistory, 'schedule') as schedule:
            response = self.client.delete(url, **self.auth_header(), **extra)
        return response, schedule

    def test_keep_files_survives_delete_files_on_disk(self):
        '''
            The MED-641 trap: media_post_delete deletes each downloaded
            video when the source has delete_files_on_disk set, and
            TubeSync's hidden copy inherits that flag. A keep-files delete
            must keep every file anyway.
        '''
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root():
            source = make_source(delete_files_on_disk=True, delete_removed_media=True)
            video = make_downloaded_media(source)
            folder = video.parent

            response, schedule = self.delete(source.pk, 'false')
            self.assertEqual(response.status_code, 202)
            body = json.loads(response.content)
            assert_matches_schema(self, body, 'SourceDeletion')
            self.assertEqual(body['uuid'], str(source.pk))
            self.assertEqual(body['canonicalKey'], KEY)
            self.assertEqual(body['directory'], FOLDER)
            self.assertFalse(body['deleteFiles'])
            self.assertEqual(body['normalizedState'], 'deleting')

            self.assertFalse(Source.objects.filter(pk=source.pk).exists())
            hidden = Source.objects.get(key=KEY)
            self.assertFalse(hidden.delete_files_on_disk)
            self.assertFalse(hidden.delete_removed_media)
            self.assertEqual(hidden.filter_text, str(source.pk))
            self.assertEqual(scheduled_deletes(schedule), 1)
            # Nothing moved: TubeSync's own rename would have moved the
            # folder into <dir>/deleted.
            self.assertTrue(video.exists())
            self.assertFalse((folder / 'deleted').exists())

            run_delete_task(hidden)

            self.assertTrue(video.exists())
            self.assertTrue(video.with_suffix('.nfo').exists())
            self.assertTrue(folder.is_dir())
            self.assertFalse((folder / 'deleted').exists())
            self.assertFalse(Source.objects.filter(key=KEY).exists())
            self.assertFalse(Media.objects.exists())
            response = self.client.get(source_url(source.pk), **self.auth_header())
            self.assertEqual(response.status_code, 404)

    def test_keep_files_is_the_default(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root():
            source = make_source(delete_files_on_disk=True)
            video = make_downloaded_media(source)
            response, _ = self.delete(source.pk)
            self.assertEqual(response.status_code, 202)
            self.assertFalse(json.loads(response.content)['deleteFiles'])
            run_delete_task(Source.objects.get(key=KEY))
            self.assertTrue(video.exists())

    def test_keep_files_drops_a_stale_marker(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root():
            source = make_source()
            video = make_downloaded_media(source)
            (video.parent / '.to_be_removed').touch()
            response, _ = self.delete(source.pk, 'false')
            self.assertEqual(response.status_code, 202)
            self.assertFalse(json.loads(response.content)['deleteFiles'])
            run_delete_task(Source.objects.get(key=KEY))
            self.assertTrue(video.exists())

    def test_delete_files_removes_the_folder(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root():
            source = make_source()
            video = make_downloaded_media(source)
            folder = video.parent
            response, _ = self.delete(source.pk, 'true')
            self.assertEqual(response.status_code, 202)
            self.assertTrue(json.loads(response.content)['deleteFiles'])
            self.assertTrue((folder / '.to_be_removed').is_file())
            run_delete_task(Source.objects.get(key=KEY))
            self.assertFalse(folder.exists())
            self.assertFalse(Source.objects.filter(key=KEY).exists())

    def test_longest_key_fits_the_temporary_rename(self):
        # A 92-character key gives a 100-character folder name; TubeSync's
        # `/deleted` and `[Deleting] ` affixes must not overflow the
        # 100-character columns, and no stray folder may be left.
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        long_key = 'UC' + 'x' * 90
        long_folder = f'acq-src-{long_key}'
        self.assertEqual(len(long_folder), 100)
        with temp_download_root():
            source = make_source(key=long_key, name=long_folder, directory=long_folder)
            video = make_downloaded_media(source)
            folder = video.parent
            seen = {}
            original_delete = Source.delete

            def spy(instance, *args, **kwargs):
                if instance.pk == source.pk:
                    seen.update(
                        key=instance.key, name=instance.name, directory=instance.directory,
                    )
                return original_delete(instance, *args, **kwargs)

            with patch.object(Source, 'delete', spy):
                response, _ = self.delete(source.pk, 'false')
            self.assertEqual(response.status_code, 202)
            for field, value in seen.items():
                self.assertLessEqual(len(value), 100, field)
            self.assertTrue(seen['directory'].endswith('/deleted'))
            self.assertTrue(seen['name'].startswith('[Deleting] '))
            self.assertTrue(video.exists())
            self.assertEqual(
                sorted(p.name for p in folder.parent.iterdir()), [long_folder],
            )

    def test_clone_does_not_fetch_channel_images(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root():
            source = make_source(copy_channel_images=True)
            make_downloaded_media(source)
            with patch('sync.signals.download_source_images') as download:
                response, _ = self.delete(source.pk, 'false')
            self.assertEqual(response.status_code, 202)
            download.assert_not_called()
            self.assertFalse(Source.objects.get(key=KEY).copy_channel_images)

    def test_deleting_state_until_the_task_finishes(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root():
            source = make_source()
            make_downloaded_media(source)
            self.delete(source.pk, 'false')
            hidden = Source.objects.get(key=KEY)

            response = self.client.get(source_url(source.pk), **self.auth_header())
            self.assertEqual(response.status_code, 200)
            body = json.loads(response.content)
            assert_matches_schema(self, body, 'Source')
            self.assertEqual(body['uuid'], str(source.pk))
            self.assertEqual(body['normalizedState'], 'deleting')
            self.assertEqual(body['canonicalKey'], KEY)

            # The hidden row is not a source for its key any more ...
            response = self.client.get(f'{SOURCES_URL}?key={KEY}', **self.auth_header())
            self.assertEqual(json.loads(response.content)['data'], [])
            # ... nor addressable by its own uuid ...
            response = self.client.get(source_url(hidden.pk), **self.auth_header())
            self.assertEqual(response.status_code, 404)
            # ... and a re-add must wait instead of adopting it.
            response = self.client.post(
                SOURCES_URL,
                data=json.dumps({
                    'sourceType': 'channel',
                    'canonicalKey': KEY,
                    'canonicalUrl': f'https://www.youtube.com/channel/{KEY}',
                    'name': FOLDER,
                    'directory': FOLDER,
                }),
                content_type='application/json',
                **self.auth_header(),
            )
            self.assertEqual(response.status_code, 409)
            body = json.loads(response.content)
            assert_matches_schema(self, body, 'Error')
            self.assertEqual(body['code'], 'SOURCE_DELETING')
            self.assertTrue(body['retryable'])
            self.assertNotIn('existingSourceUuid', body)

            # A folder delete waits too.
            response = self.client.delete(folder_url(KEY), **self.auth_header())
            self.assertEqual(response.status_code, 409)
            self.assertEqual(json.loads(response.content)['code'], 'SOURCE_DELETING')

    def test_repeat_delete_is_idempotent(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root():
            source = make_source()
            make_downloaded_media(source)
            first, schedule = self.delete(source.pk, 'false')
            self.assertEqual(first.status_code, 202)
            self.assertEqual(scheduled_deletes(schedule), 1)
            hidden_count = Source.objects.count()

            # Asking for files the second time does not change the first
            # call's decision; the body says what is in effect.
            second, schedule = self.delete(source.pk, 'true')
            self.assertEqual(second.status_code, 202)
            self.assertEqual(schedule.call_count, 0)
            self.assertFalse(json.loads(second.content)['deleteFiles'])
            self.assertEqual(Source.objects.count(), hidden_count)

            run_delete_task(Source.objects.get(key=KEY))
            third, _ = self.delete(source.pk, 'false')
            self.assertEqual(third.status_code, 404)
            self.assertEqual(json.loads(third.content)['code'], 'SOURCE_NOT_FOUND')

    def test_keep_files_then_folder_delete(self):
        '''The admin flow: remove with files kept, delete the folder later.'''
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root() as root, run_background_inline():
            source = make_source()
            video = make_downloaded_media(source)
            folder = video.parent
            self.delete(source.pk, 'false')
            run_delete_task(Source.objects.get(key=KEY))
            self.assertTrue(folder.is_dir())

            response = self.client.delete(folder_url(KEY), **self.auth_header())
            self.assertEqual(response.status_code, 200)
            body = json.loads(response.content)
            assert_matches_schema(self, body, 'FolderDeletion')
            self.assertEqual(body['canonicalKey'], KEY)
            self.assertEqual(body['folders'], [folder.relative_to(root).as_posix()])
            self.assertFalse(folder.exists())
            self.assertEqual(list((folder.parent / deletion.TRASH_DIR_NAME).iterdir()), [])

            response = self.client.delete(folder_url(KEY), **self.auth_header())
            self.assertEqual(response.status_code, 404)
            self.assertEqual(json.loads(response.content)['code'], 'FOLDER_NOT_FOUND')

    def test_ui_delete_keeps_upstream_behaviour(self):
        '''
            TubeSync's own delete page (keep_files=False) still lets the
            source's delete_files_on_disk setting delete files: the fork only
            changes what the bridge asks for.
        '''
        with temp_download_root():
            source = make_source(delete_files_on_disk=True)
            make_downloaded_media(source)
            media_source = prepare_source_for_deletion(source, delete_files=False)
            self.assertTrue(media_source.delete_files_on_disk)
            self.assertFalse(media_source.delete_removed_media)


class DeleteSourceRequestTestCase(BridgeTestCase):

    def test_unknown_uuid_is_404(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        response = self.client.delete(source_url(uuid.uuid4()), **self.auth_header())
        self.assertEqual(response.status_code, 404)
        body = json.loads(response.content)
        assert_matches_schema(self, body, 'Error')
        self.assertEqual(body['code'], 'SOURCE_NOT_FOUND')

    def test_malformed_uuid_is_404(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        response = self.client.delete(source_url('not-a-uuid'), **self.auth_header())
        self.assertEqual(response.status_code, 404)

    def test_bad_delete_files_value_is_400(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        source = make_source()
        for value in ('maybe', ''):
            with self.subTest(value=value):
                response = self.client.delete(
                    source_url(source.pk) + f'?deleteFiles={value}', **self.auth_header(),
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(json.loads(response.content)['code'], 'SOURCE_INVALID')
                self.assertTrue(Source.objects.filter(pk=source.pk).exists())

    def test_read_only_bridge_refuses(self):
        self.enable_bridge()  # MEDIANEST_BRIDGE_READ_ONLY defaults to true
        source = make_source()
        response = self.client.delete(source_url(source.pk), **self.auth_header())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(json.loads(response.content)['code'], 'PROVIDER_READ_ONLY')
        response = self.client.delete(folder_url(KEY), **self.auth_header())
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Source.objects.filter(pk=source.pk).exists())

    def test_requires_bearer_token(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        source = make_source()
        response = self.client.delete(source_url(source.pk))
        self.assertEqual(response.status_code, 401)
        self.assertTrue(Source.objects.filter(pk=source.pk).exists())

    def test_audit_line_names_the_uuid(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with self.assertLogs('tubesync', level='INFO') as logs:
            self.client.delete(source_url(uuid.uuid4()), **self.auth_header())
        self.assertTrue(any(
            'mutation_audit' in line and 'outcome=rejected_SOURCE_NOT_FOUND' in line
            for line in logs.output
        ))

    def test_capabilities(self):
        self.enable_bridge()
        body = json.loads(self.client.get(CAPS_URL, **self.auth_header()).content)
        assert_matches_schema(self, body, 'Capabilities')
        self.assertTrue(body['deleteSource'])
        self.assertTrue(body['deleteSourceFiles'])


class DeleteFolderRequestTestCase(BridgeTestCase):

    def test_live_source_is_409_with_its_uuid(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root():
            source = make_source()
            Path(source.directory_path).mkdir(parents=True)
            response = self.client.delete(folder_url(KEY), **self.auth_header())
            self.assertEqual(response.status_code, 409)
            body = json.loads(response.content)
            self.assertEqual(body['code'], 'SOURCE_CONFLICT')
            self.assertEqual(body['existingSourceUuid'], str(source.pk))
            self.assertTrue(Path(source.directory_path).is_dir())

    def test_source_with_another_key_using_the_folder_is_409(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root():
            make_source(key='PLother', name='other')
            response = self.client.delete(folder_url(KEY), **self.auth_header())
            self.assertEqual(response.status_code, 409)

    def test_invalid_keys_are_400(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        for key in ('..', 'a.b', 'UC..x', 'x' * 93, 'a b', 'UC%00', 'UCabc%0A'):
            with self.subTest(key=key):
                response = self.client.delete(folder_url(key), **self.auth_header())
                self.assertEqual(response.status_code, 400, key)
                self.assertEqual(json.loads(response.content)['code'], 'SOURCE_INVALID')

    def test_only_the_named_folder_goes(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root() as root, run_background_inline():
            video_dir, audio_dir = deletion.folder_parents()
            target = video_dir / FOLDER
            target.mkdir(parents=True)
            (target / 'a.mkv').write_bytes(b'x')
            neighbour = video_dir / f'{FOLDER}x'
            neighbour.mkdir()
            unrelated = video_dir / KEY
            unrelated.mkdir()
            in_audio = audio_dir / FOLDER
            in_audio.mkdir(parents=True)

            response = self.client.delete(folder_url(KEY), **self.auth_header())
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                sorted(json.loads(response.content)['folders']),
                sorted([
                    target.relative_to(root).as_posix(),
                    in_audio.relative_to(root).as_posix(),
                ]),
            )
            self.assertFalse(target.exists())
            self.assertFalse(in_audio.exists())
            self.assertTrue(neighbour.is_dir())
            self.assertTrue(unrelated.is_dir())

    def test_symlinked_folder_is_refused(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root() as root:
            outside = root / 'outside'
            outside.mkdir()
            (outside / 'keep.txt').write_text('keep')
            video_dir = deletion.folder_parents()[0]
            video_dir.mkdir(parents=True)
            os.symlink(outside, video_dir / FOLDER)
            response = self.client.delete(folder_url(KEY), **self.auth_header())
            self.assertEqual(response.status_code, 400)
            self.assertTrue((outside / 'keep.txt').exists())

    def test_leftover_trash_is_swept(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root(), run_background_inline():
            video_dir = deletion.folder_parents()[0]
            leftover = video_dir / deletion.TRASH_DIR_NAME / ('acq-src-old-' + '0123456789abcdef0123456789abcdef')
            leftover.mkdir(parents=True)
            (leftover / 'a.mkv').write_bytes(b'x')
            (video_dir / FOLDER).mkdir()
            response = self.client.delete(folder_url(KEY), **self.auth_header())
            self.assertEqual(response.status_code, 200)
            self.assertFalse(leftover.exists())

    def test_retry_after_interrupted_removal_sweeps_the_trash(self):
        # The folder was already renamed into the trash, then the process
        # stopped before removing it. The retry finds no folder (404) but
        # still finishes the removal.
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root(), run_background_inline():
            video_dir = deletion.folder_parents()[0]
            leftover = video_dir / deletion.TRASH_DIR_NAME / f'{FOLDER}-{'0123456789abcdef0123456789abcdef'}'
            leftover.mkdir(parents=True)
            (leftover / 'a.mkv').write_bytes(b'x')
            response = self.client.delete(folder_url(KEY), **self.auth_header())
            self.assertEqual(response.status_code, 404)
            self.assertEqual(json.loads(response.content)['code'], 'FOLDER_NOT_FOUND')
            self.assertFalse(leftover.exists())

    def test_source_naming_the_folder_another_way_is_409(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root():
            video_dir = deletion.folder_parents()[0]
            (video_dir / 'elsewhere').mkdir(parents=True)
            for index, spelling in enumerate((
                f'./{FOLDER}',
                f'elsewhere/../{FOLDER}',
                f'{FOLDER}/season1',
            )):
                with self.subTest(directory=spelling):
                    other = make_source(
                        key=f'PLother{index}', name=f'other{index}', directory=spelling,
                    )
                    Path(other.directory_path).mkdir(parents=True, exist_ok=True)
                    (video_dir / FOLDER / 'keep.mkv').write_bytes(b'x')
                    response = self.client.delete(folder_url(KEY), **self.auth_header())
                    self.assertEqual(response.status_code, 409, spelling)
                    body = json.loads(response.content)
                    self.assertEqual(body['code'], 'SOURCE_CONFLICT')
                    self.assertEqual(body['existingSourceUuid'], str(other.pk))
                    self.assertTrue((video_dir / FOLDER / 'keep.mkv').exists())
                    other.delete()

    def test_source_created_during_the_move_gets_its_folder_back(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root(), run_background_inline():
            video_dir = deletion.folder_parents()[0]
            target = video_dir / FOLDER
            target.mkdir(parents=True)
            (target / 'keep.mkv').write_bytes(b'x')
            original_delete_folders = deletion.delete_folders
            created = {}

            def racing_delete(folders, **kwargs):
                # A POST /sources for the same key lands between the
                # holder check and the rename (no signals: nothing else
                # touches the folder).
                created['source'] = Source.objects.bulk_create([Source(
                    source_type=Val(YouTube_SourceType.CHANNEL_ID),
                    key=KEY, name=FOLDER, directory=FOLDER,
                )])[0]
                return original_delete_folders(folders, **kwargs)

            with patch.object(deletion, 'delete_folders', racing_delete):
                response = self.client.delete(folder_url(KEY), **self.auth_header())
            self.assertEqual(response.status_code, 409)
            self.assertEqual(json.loads(response.content)['code'], 'SOURCE_CONFLICT')
            self.assertTrue((target / 'keep.mkv').exists())
            self.assertEqual(
                list((video_dir / deletion.TRASH_DIR_NAME).iterdir()), [],
            )

    def test_recreated_folder_keeps_the_old_contents_out_of_sweeps(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root(), run_background_inline():
            video_dir = deletion.folder_parents()[0]
            target = video_dir / FOLDER
            target.mkdir(parents=True)
            (target / 'old.mkv').write_bytes(b'x')
            original_delete_folders = deletion.delete_folders

            def claimed_and_recreated(folders, **kwargs):
                def claimed():
                    # The new source already wrote into its fresh folder.
                    target.mkdir()
                    (target / 'new.mkv').write_bytes(b'y')
                    return True
                return original_delete_folders(folders, claimed=claimed)

            with patch.object(deletion, 'delete_folders', claimed_and_recreated):
                response = self.client.delete(folder_url(KEY), **self.auth_header())
            self.assertEqual(response.status_code, 409)
            trash = video_dir / deletion.TRASH_DIR_NAME
            kept = list(trash.iterdir())
            self.assertEqual(len(kept), 1)
            self.assertTrue(kept[0].name.startswith(deletion.KEPT_PREFIX))
            self.assertEqual(deletion.leftover_trash(), [])
            deletion.sweep_leftover_trash(background=False)
            self.assertTrue((kept[0] / 'old.mkv').exists())
            self.assertTrue((target / 'new.mkv').exists())

    def test_empty_recreated_folder_is_replaced_by_the_old_one(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root(), run_background_inline():
            video_dir = deletion.folder_parents()[0]
            target = video_dir / FOLDER
            target.mkdir(parents=True)
            (target / 'old.mkv').write_bytes(b'x')
            original_delete_folders = deletion.delete_folders

            def claimed_and_recreated(folders, **kwargs):
                def claimed():
                    target.mkdir()  # check_source_directory_exists()
                    return True
                return original_delete_folders(folders, claimed=claimed)

            with patch.object(deletion, 'delete_folders', claimed_and_recreated):
                response = self.client.delete(folder_url(KEY), **self.auth_header())
            self.assertEqual(response.status_code, 409)
            self.assertTrue((target / 'old.mkv').exists())
            self.assertEqual(list((video_dir / deletion.TRASH_DIR_NAME).iterdir()), [])

    def test_sweep_leaves_an_unconfirmed_move_alone(self):
        # A concurrent request's sweep runs while this one is still
        # checking for a claim: the moved folder must survive it and go
        # back when a source claimed it.
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root(), run_background_inline():
            video_dir = deletion.folder_parents()[0]
            target = video_dir / FOLDER
            target.mkdir(parents=True)
            (target / 'old.mkv').write_bytes(b'x')
            original_delete_folders = deletion.delete_folders

            def claimed_during_sweep(folders, **kwargs):
                def claimed():
                    deletion.sweep_leftover_trash(background=False)
                    return True
                return original_delete_folders(folders, claimed=claimed)

            with patch.object(deletion, 'delete_folders', claimed_during_sweep):
                response = self.client.delete(folder_url(KEY), **self.auth_header())
            self.assertEqual(response.status_code, 409)
            self.assertTrue((target / 'old.mkv').exists())

    def test_stale_unconfirmed_move_goes_back_not_away(self):
        # A request that crashed between the move and its claim check left
        # a `pending-` entry: it is moved back, never deleted.
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root():
            video_dir = deletion.folder_parents()[0]
            trash = video_dir / deletion.TRASH_DIR_NAME
            name = f'{FOLDER}-0123456789abcdef0123456789abcdef'
            entry = trash / (deletion.PENDING_PREFIX + name)
            entry.mkdir(parents=True)
            (entry / 'old.mkv').write_bytes(b'x')
            self.assertEqual(deletion.leftover_trash(), [])
            self.assertTrue(entry.is_dir())  # fresh: still in flight
            with patch.object(deletion, 'PENDING_STALE_SECONDS', -1):
                self.assertEqual(deletion.sweep_leftover_trash(background=False), 0)
            self.assertTrue((video_dir / FOLDER / 'old.mkv').exists())
            self.assertFalse(entry.exists())

    def test_stale_unconfirmed_move_is_kept_when_the_folder_is_back(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root():
            video_dir = deletion.folder_parents()[0]
            trash = video_dir / deletion.TRASH_DIR_NAME
            name = f'{FOLDER}-0123456789abcdef0123456789abcdef'
            entry = trash / (deletion.PENDING_PREFIX + name)
            entry.mkdir(parents=True)
            (entry / 'old.mkv').write_bytes(b'x')
            (video_dir / FOLDER).mkdir()
            (video_dir / FOLDER / 'new.mkv').write_bytes(b'y')
            with patch.object(deletion, 'PENDING_STALE_SECONDS', -1):
                deletion.sweep_leftover_trash(background=False)
            self.assertTrue((trash / (deletion.KEPT_PREFIX + name) / 'old.mkv').exists())
            self.assertTrue((video_dir / FOLDER / 'new.mkv').exists())

    def test_retry_after_crash_mid_move_does_not_answer_404(self):
        # The earlier request crashed after moving the folder to a
        # `pending-` entry. The retry must not report the folder gone while
        # its media is still on disk: it puts it back, then deletes it.
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root(), run_background_inline():
            video_dir = deletion.folder_parents()[0]
            trash = video_dir / deletion.TRASH_DIR_NAME
            entry = trash / f'{deletion.PENDING_PREFIX}{FOLDER}-0123456789abcdef0123456789abcdef'
            entry.mkdir(parents=True)
            (entry / 'old.mkv').write_bytes(b'x')
            with patch.object(deletion, 'PENDING_STALE_SECONDS', -1):
                response = self.client.delete(folder_url(KEY), **self.auth_header())
            self.assertEqual(response.status_code, 200)
            self.assertFalse((video_dir / FOLDER).exists())
            self.assertEqual(list(trash.iterdir()), [])

    def test_retry_during_a_fresh_unconfirmed_move_is_retryable(self):
        # Within ten minutes of a crash (or while another request is still
        # checking its claim) the folder may come back: never a 404.
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root(), run_background_inline():
            video_dir = deletion.folder_parents()[0]
            trash = video_dir / deletion.TRASH_DIR_NAME
            entry = trash / f'{deletion.PENDING_PREFIX}{FOLDER}-0123456789abcdef0123456789abcdef'
            entry.mkdir(parents=True)
            (entry / 'old.mkv').write_bytes(b'x')
            other = trash / f'{deletion.PENDING_PREFIX}{FOLDER}x-0123456789abcdef0123456789abcdef'
            other.mkdir()
            response = self.client.delete(folder_url(KEY), **self.auth_header())
            self.assertEqual(response.status_code, 409)
            body = json.loads(response.content)
            self.assertEqual(body['code'], 'SOURCE_DELETING')
            self.assertTrue(body['retryable'])
            self.assertTrue((entry / 'old.mkv').exists())
            # another key's pending move does not block this one
            self.assertEqual(deletion.pending_moves(KEY), [entry])

    def test_retry_after_crash_mid_move_still_refuses_a_claimed_folder(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root():
            video_dir = deletion.folder_parents()[0]
            entry = video_dir / deletion.TRASH_DIR_NAME / (
                f'{deletion.PENDING_PREFIX}{FOLDER}-0123456789abcdef0123456789abcdef'
            )
            entry.mkdir(parents=True)
            (entry / 'old.mkv').write_bytes(b'x')
            make_source()
            with patch.object(deletion, 'PENDING_STALE_SECONDS', -1):
                response = self.client.delete(folder_url(KEY), **self.auth_header())
            self.assertEqual(response.status_code, 409)
            self.assertTrue((video_dir / FOLDER / 'old.mkv').exists())

    def test_trash_sweep_leaves_foreign_entries(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root(), run_background_inline():
            video_dir = deletion.folder_parents()[0]
            foreign = video_dir / deletion.TRASH_DIR_NAME / 'someone-elses-folder'
            foreign.mkdir(parents=True)
            (video_dir / FOLDER).mkdir()
            response = self.client.delete(folder_url(KEY), **self.auth_header())
            self.assertEqual(response.status_code, 200)
            self.assertTrue(foreign.is_dir())

    def test_video_dir_symlinked_out_of_the_root_is_refused(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root(), tempfile.TemporaryDirectory() as outside:
            outside = Path(outside)
            (outside / FOLDER).mkdir()
            (outside / FOLDER / 'keep.mkv').write_bytes(b'x')
            video_dir = deletion.folder_parents()[0]
            video_dir.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(outside, video_dir)
            response = self.client.delete(folder_url(KEY), **self.auth_header())
            self.assertEqual(response.status_code, 400)
            self.assertTrue((outside / FOLDER / 'keep.mkv').exists())

    def test_without_directory_prefix_uses_the_root(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with (
            temp_download_root() as root,
            run_background_inline(),
            override_settings(SOURCE_DOWNLOAD_DIRECTORY_PREFIX=False),
        ):
            (root / FOLDER).mkdir()
            response = self.client.delete(folder_url(KEY), **self.auth_header())
            self.assertEqual(response.status_code, 200)
            self.assertEqual(json.loads(response.content)['folders'], [FOLDER])

    def test_audit_line_names_the_key(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with temp_download_root(), self.assertLogs('tubesync', level='INFO') as logs:
            self.client.delete(folder_url(KEY), **self.auth_header())
        self.assertTrue(any(
            'mutation_audit' in line and f'source_key={KEY}' in line
            and 'outcome=rejected_FOLDER_NOT_FOUND' in line
            for line in logs.output
        ))
