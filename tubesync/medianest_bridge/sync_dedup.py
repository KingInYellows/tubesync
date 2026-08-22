'''
    Bridge-side dedup for POST /sources/{sourceUuid}/sync, per the
    contract's own syncSource description: a check against only
    "actively running" index_source tasks (sync.tasks.get_source_index_task,
    which wraps TaskHistoryQuerySet.running()) is insufficient -- a task
    that has been enqueued/scheduled but not yet picked up by a huey
    worker is invisible to that check, and calling sync-now again would
    schedule a second, redundant index_source run.

    find_pending_or_running_index_task() below is derived from reading
    common/huey.py's register_huey_signals() handler directly (not
    guessed): a TaskHistory row's start_at is set ONLY when the
    EXECUTING signal fires; end_at is touched by every signal fired for
    that task. So:
      - start_at IS NULL            -> enqueued/scheduled, not started yet
      - start_at == end_at          -> actively executing (nothing has
                                        touched end_at since EXECUTING)
      - start_at set, != end_at     -> a later signal (COMPLETE/ERROR)
                                        moved end_at forward -> terminal

    "non-completed" is the union of the first two states. Bounded to
    MAX_RUN_TIME (matching sync.tasks.get_running_tasks()'s own
    staleness window) so a worker that crashed mid-execution without
    ever firing a terminal signal cannot permanently block sync-now for
    a source.

    Per the contract's own caveat ("This predicate is a T3 implementation
    detail pending dynamic verification... this operation MUST NOT be
    assumed to be a no-op-safe idempotent call until that verification
    lands"): this is a static reading of the signal-handler code, not a
    dynamically-verified guarantee against a live huey consumer. Treat it
    as best-effort dedup, not a proven idempotency guarantee.
'''
from django.conf import settings
from django.db.models import F, Q
from django.utils import timezone

from common.models import TaskHistory
from sync.tasks import get_model_tasks

INDEX_SOURCE_TASK_NAME = 'sync.tasks.index_source'


def find_pending_or_running_index_task(source_uuid_str):
    max_run_time = getattr(settings, 'MAX_RUN_TIME', 3600)
    cutoff = timezone.now() - timezone.timedelta(seconds=max_run_time)
    base = TaskHistory.objects.filter(
        Q(start_at__isnull=True) | Q(start_at=F('end_at')),
        end_at__gt=cutoff,
    )
    tqs = get_model_tasks(source_uuid_str, name=INDEX_SOURCE_TASK_NAME, qs=base)
    return tqs.first()
