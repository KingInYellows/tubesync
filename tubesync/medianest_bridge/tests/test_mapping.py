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

    def test_error_field_is_sanitized_end_to_end(self):
        '''
            T4: confirms error_sanitize.sanitize_error_message() is
            actually wired into serialize_media(), not just unit-tested
            in isolation -- constructs a real TaskHistory row matching
            get_relevant_media_download_task()'s fallback-tier lookup
            (name ending in download_media_file, task_params starting with
            the media pk, failed_at == end_at so _task_failure_is_current()
            treats the failure as current) with a path-bearing last_error,
            and asserts the path never reaches serialize_media()'s output.
        '''
        from django.conf import settings

        source = make_source()
        media = make_media(source)
        failure_signal_at = timezone.now()
        make_download_task(
            media,
            start_at=timezone.now() - timezone.timedelta(seconds=30),
            end_at=failure_signal_at,
            scheduled_at=failure_signal_at,
            failed_at=failure_signal_at,
            last_error=(
                f"[Errno 2] No such file or directory: "
                f"'{settings.DOWNLOAD_ROOT}/leaked/path.mp4'"
            ),
        )
        body = mapping.serialize_media(media)
        self.assertIsNotNone(body['error'])
        self.assertNotIn(str(settings.DOWNLOAD_ROOT), body['error'])
        self.assertNotIn('leaked', body['error'])
        self.assertIn('<redacted>', body['error'])


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

    def test_exhausted_failed_task_maps_to_failed_with_error_no_retry(self):
        # An automatic download_media_file failure (no retries=
        # configured for that call) never gets its scheduled_at pushed
        # forward -- it stays at whenever that attempt itself ran, in
        # the past. retryAt must be null: there is no pending retry.
        source = make_source()
        media = make_media(source)
        past = timezone.now() - timezone.timedelta(seconds=30)
        # historical_task() sets failed_at and end_at from the same
        # signal_dt in a single SIGNAL_ERROR call -- share one timestamp
        # here too, so this fixture matches production shape rather than
        # incidentally passing _task_failure_is_current()'s equality
        # check via two nearly-equal but distinct timezone.now() calls.
        failure_signal_at = timezone.now()
        make_download_task(
            media,
            start_at=past,
            end_at=failure_signal_at,
            scheduled_at=past,
            failed_at=failure_signal_at,
            last_error='RuntimeError: network unreachable',
        )
        body = mapping.serialize_media(media)
        self.assertEqual(body['normalizedState'], 'failed')
        self.assertIsNone(body['retryAt'])
        self.assertEqual(body['error'], 'network unreachable')

    def test_failed_task_with_a_genuine_pending_retry_reports_retry_at(self):
        # sync/views/media.py's MediaRedownloadView schedules
        # download_media_file with retries=3, retry_delay=600 -- huey's
        # own retry mechanism reschedules the SAME row to a real future
        # scheduled_at after a failure, without clearing failed_at/
        # last_error (T2 review P2 finding: "fresh evidence beyond the
        # earlier terminal-failure case").
        source = make_source()
        media = make_media(source)
        past = timezone.now() - timezone.timedelta(seconds=30)
        retry_at = timezone.now() + timezone.timedelta(seconds=300)
        failure_signal_at = timezone.now()
        make_download_task(
            media,
            start_at=past,
            end_at=failure_signal_at,
            scheduled_at=retry_at,
            failed_at=failure_signal_at,
            last_error='RuntimeError: network unreachable',
        )
        body = mapping.serialize_media(media)
        self.assertEqual(body['normalizedState'], 'failed')
        self.assertIsNotNone(body['retryAt'])
        self.assertEqual(body['error'], 'network unreachable')

    def test_pending_retry_after_scheduled_signal_keeps_failed_error_and_retry_at(self):
        # Huey SIGNAL_SCHEDULED after ERROR advances end_at on the same
        # row without clearing failed_at. That is the real retry window,
        # not the simplified failed_at == end_at fixture above.
        source = make_source()
        media = make_media(source)
        past = timezone.now() - timezone.timedelta(seconds=30)
        retry_at = timezone.now() + timezone.timedelta(seconds=300)
        failure_signal_at = timezone.now() - timezone.timedelta(seconds=5)
        scheduled_signal_at = timezone.now()
        make_download_task(
            media,
            start_at=past,
            end_at=scheduled_signal_at,
            scheduled_at=retry_at,
            failed_at=failure_signal_at,
            last_error='RuntimeError: network unreachable',
        )
        body = mapping.serialize_media(media)
        self.assertEqual(body['normalizedState'], 'failed')
        self.assertIsNotNone(body['retryAt'])
        self.assertEqual(body['error'], 'network unreachable')

    def test_stale_error_cleared_after_a_same_row_retry_succeeds(self):
        # P2 review finding: huey's retries= mechanism reuses the same
        # task_id/row across attempts (see the retryAt docstring above).
        # historical_task() sets failed_at/last_error only on a
        # SIGNAL_ERROR and never clears them -- so a row that failed
        # once and then succeeded on a later retry (same row, a
        # subsequent SIGNAL_COMPLETE) still has last_error populated
        # from the earlier attempt. end_at, unlike failed_at, is touched
        # by every signal including that final success, so end_at is
        # now well after failed_at -- serialize_media() must not surface
        # the stale error once media.downloaded is true.
        source = make_source()
        media = make_media(source, downloaded=True)
        first_attempt_failed_at = timezone.now() - timezone.timedelta(seconds=120)
        later_success_at = timezone.now()
        make_download_task(
            media,
            start_at=timezone.now() - timezone.timedelta(seconds=130),
            end_at=later_success_at,
            scheduled_at=timezone.now() - timezone.timedelta(seconds=130),
            failed_at=first_attempt_failed_at,
            last_error='RuntimeError: network unreachable',
        )
        body = mapping.serialize_media(media)
        self.assertEqual(body['normalizedState'], 'downloaded')
        self.assertIsNone(body['error'])
        self.assertIsNone(body['retryAt'])

    def test_failed_task_does_not_mask_skipped_state(self):
        # T2 review P2 finding: a stale failed TaskHistory row must not
        # keep reporting "failed" once media is later marked skipped --
        # get_download_state()'s own skip check is unreachable whenever
        # any task is passed in, so without this, "failed" would persist
        # until task-history cleanup (normally up to 30 days).
        source = make_source()
        media = make_media(source, skip=True, manual_skip=True)
        make_download_task(
            media,
            start_at=timezone.now() - timezone.timedelta(seconds=30),
            end_at=timezone.now(),
            failed_at=timezone.now(),
            last_error='RuntimeError: network unreachable',
        )
        body = mapping.serialize_media(media)
        self.assertEqual(body['normalizedState'], 'skipped')
        self.assertIsNone(body['error'])
        self.assertIsNone(body['retryAt'])

    def test_failed_task_does_not_mask_ineligible_state(self):
        source = make_source(download_media=False)
        media = make_media(source)
        make_download_task(
            media,
            start_at=timezone.now() - timezone.timedelta(seconds=30),
            end_at=timezone.now(),
            failed_at=timezone.now(),
            last_error='RuntimeError: network unreachable',
        )
        body = mapping.serialize_media(media)
        self.assertEqual(body['normalizedState'], 'ineligible')
        self.assertIsNone(body['error'])

    def test_pending_task_does_not_mask_skipped_state(self):
        # T2 review P2 finding: sync/models/media__tasks.py's
        # download_checklist() rejects a still-pending download the
        # moment it starts if media.skip is set -- reporting "queued"
        # for that no-op-in-waiting is as misleading as reporting
        # "failed" for a stale terminal failure was.
        source = make_source()
        media = make_media(source, skip=True, manual_skip=True)
        make_download_task(
            media, start_at=None,
            scheduled_at=timezone.now() + timezone.timedelta(seconds=60),
        )
        body = mapping.serialize_media(media)
        self.assertEqual(body['normalizedState'], 'skipped')

    def test_pending_task_does_not_mask_ineligible_state(self):
        source = make_source(download_media=False)
        media = make_media(source)
        make_download_task(
            media, start_at=None,
            scheduled_at=timezone.now() + timezone.timedelta(seconds=60),
        )
        body = mapping.serialize_media(media)
        self.assertEqual(body['normalizedState'], 'ineligible')

    def test_override_pending_task_survives_skip_suppression(self):
        # download_media_file(media_id, override=True) forwards straight
        # into download_checklist(skip_checks=True), bypassing the
        # media.skip rejection entirely -- exactly what
        # MediaRedownloadView relies on. Such a task genuinely will still
        # download despite skip, so it must not be suppressed the way a
        # plain pending task is.
        source = make_source()
        media = make_media(source, skip=True, manual_skip=True)
        make_download_task(
            media, start_at=None,
            scheduled_at=timezone.now() + timezone.timedelta(seconds=60),
            task_params=[[str(media.pk)], repr({'override': True})],
        )
        body = mapping.serialize_media(media)
        self.assertEqual(body['normalizedState'], 'queued')

    def test_running_task_state_is_not_affected_by_skip(self):
        # Guards the "mutually exclusive" reasoning in serialize_media():
        # has_error() and currently-running are exclusive on one row, so
        # this suppression must never apply to (and can never mask) an
        # actively downloading task, even if skip is somehow also set.
        source = make_source()
        media = make_media(source, skip=True, manual_skip=True)
        make_download_task(media)  # defaults are the running() shape
        body = mapping.serialize_media(media)
        self.assertEqual(body['normalizedState'], 'downloading')

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

    def test_fallback_query_is_scoped_to_the_requested_ids(self):
        '''
            T2 review P2 finding: a page containing even one media with no
            pending/failed task must not fall back to scanning every
            pending/failed download_media_file row across the whole
            deployment -- the query itself must be constrained to the
            requested media_ids, not just filtered in Python. An
            unrelated source's failed task (outside this batch's id_set)
            proves the fallback tier is DB-scoped: if it leaked in, this
            test's idle_media would incorrectly resolve to that
            unrelated task instead of staying False.
        '''
        source = make_source()
        other_source = make_source(key='UC_other', name='Other', directory='other')
        idle_media = make_media(source, key='idle')
        unrelated_media = make_media(other_source, key='unrelated')
        make_download_task(
            unrelated_media,
            failed_at=timezone.now(),
            last_error='RuntimeError: unrelated failure',
        )
        result = mapping.batch_media_download_tasks([idle_media.pk])
        self.assertFalse(result[str(idle_media.pk)])

    def test_revoked_pending_task_is_excluded_not_reported_as_queued(self):
        '''
            T2 review P1 finding: a download cancelled through
            sync/views/tasks.py's RevokeTaskView is marked [revoked] in
            verbose_name but still has start_at IS NULL -- exactly the
            shape of a genuinely pending task. Without excluding the
            [revoked] prefix, this row would be picked up by the
            fallback tier and the media would incorrectly report
            "queued" forever, even though the cancelled task will never
            run.
        '''
        source = make_source()
        media = make_media(source)
        make_download_task(
            media,
            start_at=None,
            scheduled_at=timezone.now() + timezone.timedelta(seconds=60),
            verbose_name='[revoked] Download media "video1"',
        )
        result = mapping.batch_media_download_tasks([media.pk])
        self.assertFalse(result[str(media.pk)])
        body = mapping.serialize_media(media)
        self.assertEqual(body['normalizedState'], 'discovered')

    def test_serialize_media_does_not_requery_for_a_batched_running_task(self):
        '''
            T2 review P2 finding: Media.get_download_state(task) checks
            hasattr(task, 'locked_by_pid_running') to decide whether to
            trust the task object's own answer; a plain TaskHistory row
            never has that attribute, so without binding it here every
            call fell back to querying get_media_download_task() again --
            even after batch_media_download_tasks() had already
            determined the task was running. With the attribute bound,
            serializing a page's worth of running/failed/pending media
            costs no additional queries beyond the two batch lookups.
        '''
        source = make_source()
        running_media = make_media(source, key='running')
        failed_media = make_media(source, key='failed')
        pending_media = make_media(source, key='pending')
        make_download_task(running_media)
        make_download_task(
            failed_media,
            start_at=timezone.now() - timezone.timedelta(seconds=30),
            end_at=timezone.now(),
            failed_at=timezone.now(),
            last_error='RuntimeError: boom',
        )
        make_download_task(
            pending_media,
            start_at=None,
            scheduled_at=timezone.now() + timezone.timedelta(seconds=60),
        )
        media_rows = [running_media, failed_media, pending_media]

        with self.assertNumQueries(2):
            # Query 1: running tier. Query 2: pending/failed fallback
            # tier. Neither media.get_download_state() call below should
            # add a query if locked_by_pid_running was bound correctly.
            download_tasks = mapping.batch_media_download_tasks(
                [m.pk for m in media_rows],
            )
            bodies = [
                mapping.serialize_media(
                    m, download_task=download_tasks.get(str(m.pk), False),
                )
                for m in media_rows
            ]
        states = {body['id']: body['normalizedState'] for body in bodies}
        self.assertEqual(states[str(running_media.pk)], 'downloading')
        self.assertEqual(states[str(failed_media.pk)], 'failed')
        self.assertEqual(states[str(pending_media.pk)], 'queued')


