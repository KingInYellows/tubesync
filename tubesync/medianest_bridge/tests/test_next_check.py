'''
    Contract 1.6.1 (MED-816): Source.nextCheckAt, from next_check.py.
'''
import json
import tempfile
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

from django.test import override_settings
from django_huey import get_queue

from common.models import TaskHistory
from sync.choices import IndexSchedule, TaskQueue, Val

from .. import next_check
from .base import BridgeTestCase
from .test_endpoints import assert_matches_schema
from .test_source_controls import make_source, source_url

HOURLY = Val(IndexSchedule.EVERY_HOUR)
SIX_HOURS = Val(IndexSchedule.EVERY_6_HOURS)
DAILY = Val(IndexSchedule.EVERY_24_HOURS)
WEEKLY = Val(IndexSchedule.EVERY_7_DAYS)

NOW = datetime(2026, 11, 28, 14, 20, 0, tzinfo=dt_timezone.utc)


def at(hours=0, minutes=0):
    return NOW + timedelta(hours=hours, minutes=minutes)


INDEX_QUEUE = get_queue(Val(TaskQueue.LIMIT))


def queue_index(source, *, scheduled_at, recorded_at=None, **fields):
    return TaskHistory.objects.create(
        name='sync.tasks.index_source',
        task_id=str(uuid.uuid4()),
        queue=INDEX_QUEUE.name,
        task_params=[[str(source.pk)], '{}'],
        scheduled_at=scheduled_at,
        end_at=recorded_at or NOW,
        start_at=None,
        **fields,
    )


class NextCheckTestCase(BridgeTestCase):

    def setUp(self):
        super().setUp()
        config_dir = tempfile.TemporaryDirectory()
        self.addCleanup(config_dir.cleanup)
        ctx = override_settings(CONFIG_BASE_DIR=Path(config_dir.name))
        ctx.enable()
        self.addCleanup(ctx.disable)

    def source(self, **fields):
        source = make_source(**fields)
        # Creating a source queues its first index; these tests queue
        # their own.
        TaskHistory.objects.all().delete()
        # Source.target_schedule defaults to its creation time.
        updates = {k: fields[k] for k in ('target_schedule', 'last_crawl') if k in fields}
        if updates:
            type(source).objects.filter(pk=source.pk).update(**updates)
            source.refresh_from_db()
        return source

    def test_daily_source_waits_for_the_sweep_after_its_target_hour(self):
        # Crawled at 03:10, target 03:00 tomorrow. The 01:59 sweep looks
        # ahead to 03:00 and skips (target not before it); the 02:59 sweep
        # queues the index 5 minutes later.
        source = self.source(
            index_schedule=DAILY,
            last_crawl=NOW.replace(hour=3, minute=10),
            target_schedule=(NOW + timedelta(days=1)).replace(hour=3, minute=0),
        )
        expected = (NOW + timedelta(days=1)).replace(hour=3, minute=4)
        self.assertEqual(next_check.next_check_at(source, now=NOW), expected)

    def test_hourly_source_overdue_is_picked_up_by_the_next_sweep(self):
        source = self.source(index_schedule=HOURLY, last_crawl=at(hours=-3), target_schedule=at(hours=-2))
        self.assertEqual(next_check.next_check_at(source, now=NOW), NOW.replace(minute=59) + timedelta(minutes=5))

    def test_queued_index_sooner_than_the_sweep_wins(self):
        source = self.source(index_schedule=WEEKLY, last_crawl=at(hours=-1), target_schedule=at(hours=160))
        queue_index(source, scheduled_at=at(minutes=10))
        self.assertEqual(next_check.next_check_at(source, now=NOW), at(minutes=10))

    def test_queued_index_later_than_the_sweep_loses(self):
        source = self.source(index_schedule=HOURLY, last_crawl=at(hours=-2), target_schedule=at(hours=-1))
        queue_index(source, scheduled_at=at(hours=5))
        self.assertEqual(next_check.next_check_at(source, now=NOW), NOW.replace(minute=59) + timedelta(minutes=5))

    def test_rows_huey_holds_as_revoked_are_ignored(self):
        # on_executing_remove_duplicates() revoked it without marking it.
        source = self.source(index_schedule=WEEKLY, last_crawl=at(hours=-1), target_schedule=at(hours=160))
        row = queue_index(source, scheduled_at=at(minutes=10), recorded_at=at(hours=-2))
        INDEX_QUEUE.revoke_by_id(row.task_id, revoke_once=True)
        self.addCleanup(INDEX_QUEUE.restore_by_id, row.task_id)
        self.assertGreater(next_check.next_check_at(source, now=NOW), at(hours=100))
        # Peeking leaves the revocation for the worker.
        self.assertTrue(INDEX_QUEUE.is_revoked(row.task_id, peek=True))

    def test_live_row_recorded_before_the_last_crawl_counts(self):
        # The first index queued at create, still waiting after a sync-now
        # from TubeSync's own UI (which does not remove duplicates).
        source = self.source(index_schedule=WEEKLY, last_crawl=at(minutes=-1), target_schedule=at(hours=160))
        queue_index(source, scheduled_at=at(minutes=8), recorded_at=at(minutes=-2))
        self.assertEqual(next_check.next_check_at(source, now=NOW), at(minutes=8))

    def test_rows_the_worker_already_dequeued_are_ignored(self):
        # A revoked duplicate the worker dropped at its eta: the REVOKED
        # signal moved end_at past both the last crawl and scheduled_at.
        source = self.source(index_schedule=WEEKLY, last_crawl=at(hours=-2), target_schedule=at(hours=160))
        queue_index(source, scheduled_at=at(minutes=-30), recorded_at=at(minutes=-29))
        self.assertGreater(next_check.next_check_at(source, now=NOW), at(hours=100))

    def test_overdue_row_still_waiting_for_a_worker_counts(self):
        source = self.source(index_schedule=WEEKLY, last_crawl=at(hours=-2), target_schedule=at(hours=160))
        queue_index(source, scheduled_at=at(minutes=-5), recorded_at=at(minutes=-10))
        self.assertEqual(next_check.next_check_at(source, now=NOW), at(minutes=-5))

    def test_revoked_and_failed_rows_are_ignored(self):
        source = self.source(index_schedule=WEEKLY, last_crawl=at(hours=-1), target_schedule=at(hours=160))
        queue_index(source, scheduled_at=at(minutes=10), verbose_name='[revoked] Index media')
        queue_index(source, scheduled_at=at(minutes=20), failed_at=NOW)
        self.assertGreater(next_check.next_check_at(source, now=NOW), at(hours=100))

    def test_other_sources_queued_rows_are_ignored(self):
        source = self.source(index_schedule=WEEKLY, last_crawl=at(hours=-1), target_schedule=at(hours=160))
        other = self.source(
            index_schedule=WEEKLY, key='UCzyxwvutsrqponmlkjihgfe',
            name='acq-src-other', directory='acq-src-other',
        )
        queue_index(other, scheduled_at=at(minutes=10))
        self.assertGreater(next_check.next_check_at(source, now=NOW), at(hours=100))

    def test_never_crawled_source_reports_its_first_queued_index(self):
        source = self.source(index_schedule=SIX_HOURS)
        queue_index(source, scheduled_at=at(minutes=10))
        self.assertEqual(next_check.next_check_at(source, now=NOW), at(minutes=10))

    def test_no_check_while_indexing(self):
        source = self.source(index_schedule=HOURLY)
        self.assertIsNone(next_check.next_check_at(source, index_task=object(), now=NOW))

    def test_no_check_for_a_paused_or_inactive_source(self):
        never = Val(IndexSchedule.NEVER)
        self.assertIsNone(next_check.next_check_at(self.source(index_schedule=never), now=NOW))
        inactive = self.source(
            index_schedule=HOURLY, download_media=False, index_videos=False, index_streams=False,
            key='UCzyxwvutsrqponmlkjihgfe', name='acq-src-off', directory='acq-src-off',
        )
        self.assertIsNone(next_check.next_check_at(inactive, now=NOW))

    def test_daily_source_on_the_hour_is_queued_by_the_sweep_at_its_boundary(self):
        # Crawled at 02:10, target 03:00 tomorrow. Both predicates put the
        # boundary at 01:59:00; the sweep reads now() a moment after that,
        # so it queues the index for 02:04, not an hour later.
        source = self.source(
            index_schedule=DAILY,
            last_crawl=NOW.replace(hour=2, minute=10),
            target_schedule=(NOW + timedelta(days=1)).replace(hour=3, minute=0),
        )
        expected = (NOW + timedelta(days=1)).replace(hour=2, minute=4)
        self.assertEqual(next_check.next_check_at(source, now=NOW), expected)

    def test_sweep_exactly_on_the_minute_counts(self):
        on_the_minute = NOW.replace(minute=59)
        self.assertEqual(next_check._first_sweep_after(on_the_minute), on_the_minute)
        self.assertEqual(
            next_check._first_sweep_after(on_the_minute + timedelta(microseconds=1)),
            on_the_minute + timedelta(hours=1),
        )

    def test_source_read_reports_next_check_at(self):
        self.enable_bridge()
        source = self.source(index_schedule=HOURLY)
        queue_index(source, scheduled_at=datetime.now(dt_timezone.utc) + timedelta(minutes=10))
        response = self.client.get(source_url(source.pk), **self.auth_header())
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        assert_matches_schema(self, body, 'Source')
        self.assertRegex(body['nextCheckAt'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$')
