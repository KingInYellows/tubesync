'''
    Contract 1.6.1 (MED-816): when TubeSync next looks a source over for
    new videos, as `Source.nextCheckAt`.

    TubeSync has no single "next run" value. A source is indexed by
    whichever of these comes first (read from sync/tasks.py and
    sync/signals.py, not guessed):

    1. An index_source task already queued for it: sync-now, the first
       index after create, or the one sync/signals.py's source_pre_save
       queues at Source.task_run_at_dt when the schedule changes.
    2. The hourly schedule_indexing() sweep (crontab minute 59, strict),
       which queues index_source with a 300 s delay once both
       target_schedule < sweep + 61 min and
       last_crawl < sweep + 61 min - index_schedule.

    When either one starts, common/huey.py's on_executing_remove_duplicates()
    revokes the other queued copies, but leaves their TaskHistory rows
    looking pending (start_at NULL, no [revoked] prefix). Such a row was
    recorded before the index that revoked it, so only pending rows
    recorded after the last crawl count. A stale row that slips through
    only makes the reported time early, never late, and the next index
    corrects it.

    The value is an estimate to the minute for the sweep and exact for a
    queued task. It is null when no check is coming: a paused or inactive
    source, a video list, or an index that is running right now.
'''
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from common.models import TaskHistory
from sync.choices import IndexSchedule, Val
from sync.tasks import get_model_tasks

from .pause import is_paused
from .sync_dedup import INDEX_SOURCE_TASK_NAME
from .videolist import is_videolist

_REVOKED_VERBOSE_PREFIX = '[revoked] '
# schedule_indexing(): crontab(minute=59) and next_hour = now + 1 h 1 min.
SWEEP_MINUTE = 59
SWEEP_LOOKAHEAD = timedelta(hours=1, minutes=1)
# schedule_indexing() queues index_source with delay=300.
SWEEP_DELAY = timedelta(seconds=300)


def _first_sweep_after(moment):
    '''The first schedule_indexing() run strictly after `moment`.'''
    sweep = moment.replace(minute=SWEEP_MINUTE, second=0, microsecond=0)
    if sweep <= moment:
        sweep += timedelta(hours=1)
    return sweep


def sweep_check_at(source, *, now):
    '''When schedule_indexing() would next queue an index for `source`.'''
    floor = now
    if source.target_schedule is not None:
        floor = max(floor, source.target_schedule - SWEEP_LOOKAHEAD)
    if source.last_crawl is not None:
        floor = max(
            floor,
            source.last_crawl + timedelta(seconds=source.index_schedule) - SWEEP_LOOKAHEAD,
        )
    return _first_sweep_after(floor) + SWEEP_DELAY


def pending_check_at(source, *, now):
    '''The soonest index_source task queued for `source`, or None.'''
    max_run_time = getattr(settings, 'MAX_RUN_TIME', 3600)
    qs = TaskHistory.objects.exclude(
        verbose_name__startswith=_REVOKED_VERBOSE_PREFIX,
    ).filter(
        start_at__isnull=True,
        failed_at__isnull=True,
        scheduled_at__gt=now - timedelta(seconds=max_run_time),
    )
    if source.last_crawl is not None:
        qs = qs.filter(Q(end_at__gt=source.last_crawl))
    qs = get_model_tasks(str(source.pk), name=INDEX_SOURCE_TASK_NAME, qs=qs)
    task = qs.order_by('scheduled_at').only('scheduled_at').first()
    return task.scheduled_at if task else None


def next_check_at(source, *, index_task=None, now=None):
    '''
        `index_task` is the running task serialize_source() already looked
        up (sync.tasks.get_source_index_task), so a source being indexed
        right now reports null rather than a time after it.
    '''
    if index_task:
        return None
    if is_videolist(source) or is_paused(source) or not source.is_active:
        return None
    if source.index_schedule == Val(IndexSchedule.NEVER):
        return None
    if now is None:
        now = timezone.now()
    sweep = sweep_check_at(source, now=now)
    pending = pending_check_at(source, now=now)
    if pending is not None and pending < sweep:
        return pending
    return sweep
