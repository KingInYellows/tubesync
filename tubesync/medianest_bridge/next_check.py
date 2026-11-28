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
    revokes the other queued copies in huey's storage, but leaves their
    TaskHistory rows looking pending (start_at NULL, no [revoked] prefix).
    So a row counts only while huey neither holds its task as revoked nor
    has a revoked or expired signal in the task's history, which
    historical_task() keeps in huey's storage. Both reads peek. The row's
    own times say nothing here: end_at moves on every signal, including
    the scheduler's ENQUEUED for a due task still waiting for a worker,
    and sync-now from TubeSync's own UI does not remove duplicates, so a
    row from before the last crawl can still be live.

    The value is an estimate to the minute for the sweep and exact for a
    queued task. It is null when no check is coming: a paused or inactive
    source, a video list, or an index that is running right now.
'''
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django_huey import DJANGO_HUEY, get_queue
from huey import signals as huey_signals

from common.huey import storage_key_prefix as TASK_HISTORY_KEY_PREFIX
from common.models import TaskHistory
from sync.choices import IndexSchedule, Val
from sync.tasks import get_model_tasks

from .pause import is_paused
from .sync_dedup import INDEX_SOURCE_TASK_NAME
from .videolist import is_videolist

_REVOKED_VERBOSE_PREFIX = '[revoked] '
# A source rarely has more than a couple of index tasks queued.
_MAX_PENDING_ROWS = 10
# schedule_indexing(): crontab(minute=59) and next_hour = now + 1 h 1 min.
SWEEP_MINUTE = 59
SWEEP_LOOKAHEAD = timedelta(hours=1, minutes=1)
# schedule_indexing() queues index_source with delay=300.
SWEEP_DELAY = timedelta(seconds=300)
# How long after :59:00 the sweep is assumed to read timezone.now().
SWEEP_START_GRACE = timedelta(seconds=10)


def _first_sweep_after(moment):
    '''
        The first schedule_indexing() run that executes after `moment`.

        The sweep's crontab fires at :59:00, and it reads timezone.now()
        a moment after that, so its strict predicates pass for a boundary
        a little past :59:00. SWEEP_START_GRACE is how far past counts.
    '''
    sweep = moment.replace(minute=SWEEP_MINUTE, second=0, microsecond=0)
    if sweep + SWEEP_START_GRACE <= moment:
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


def _huey_queues():
    names = (DJANGO_HUEY or {}).get('queues', {})
    return {q.name: q for q in map(get_queue, names)}


# historical_task() records these in the task's huey history when the
# worker drops the task instead of running it.
_DROPPED_SIGNALS = (huey_signals.SIGNAL_REVOKED, huey_signals.SIGNAL_EXPIRED)


def _is_dropped(task, queues):
    '''
        Peeks only, so the worker still drops a revoked task, and the
        history stays for the signals after it.
    '''
    queue = queues.get(task.queue)
    if queue is None:
        return False
    if queue.is_revoked(task.task_id, peek=True):
        return True
    history = queue.get(key=f'{TASK_HISTORY_KEY_PREFIX}{task.task_id}', peek=True)
    return isinstance(history, dict) and any(s in history for s in _DROPPED_SIGNALS)


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
    qs = get_model_tasks(str(source.pk), name=INDEX_SOURCE_TASK_NAME, qs=qs)
    queues = None
    for task in qs.order_by('scheduled_at').only('scheduled_at', 'task_id', 'queue')[:_MAX_PENDING_ROWS]:
        if queues is None:
            queues = _huey_queues()
        if not _is_dropped(task, queues):
            return task.scheduled_at
    return None


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
