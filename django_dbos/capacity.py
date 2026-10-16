"""Worker capacity diagnostics for django-dbos.

Everything here comes from the DBOS system database, read through the public
``DBOSClient`` API (never by launching DBOS): the *active* workflows — the
backlog being measured — are listed and folded into per-queue and
per-executor figures, plus worker heartbeats pushed by the workers themselves
into the Django database (see ``heartbeat.py``).

Each queue gets a scaling *advice* (SCALE_UP / SCALE_DOWN / OK) from
``advise_queue``, a deliberately simple heuristic whose thresholds come
from ``OPTIONS["CAPACITY"]`` (globally) and ``QUEUES[name]["capacity"]``
(per queue); ``CAPACITY["ADVISOR"]`` or ``collect(advisor=...)`` replaces it
entirely. ``collect`` is a snapshot: smoothing several samples before
acting on a SCALE_DOWN is the caller's job.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from dbos import WorkflowStatusString

from .client import get_client
from .heartbeat import read_heartbeats
from .settings import CAPACITY_THRESHOLD_DEFAULTS, resolve_capacity, resolve_queues

# ``(queue_name, metrics, thresholds) -> advice`` — the signature of a custom advisor.
Advisor = Callable[[str, dict[str, Any], dict[str, float | int]], str]

# Default thresholds of ``advise_queue`` (overridable through settings).
DEFAULT_SCALE_UP_BACKLOG_AGE_SECONDS = CAPACITY_THRESHOLD_DEFAULTS['SCALE_UP_BACKLOG_AGE_SECONDS']
DEFAULT_SCALE_DOWN_MAX_RUNNING = CAPACITY_THRESHOLD_DEFAULTS['SCALE_DOWN_MAX_RUNNING']

# DBOS has no RUNNING status: ENQUEUED waits for a worker, PENDING is being
# executed, DELAYED waits for its ``run_after`` time.
ACTIVE_STATUSES = (
    WorkflowStatusString.ENQUEUED.value,
    WorkflowStatusString.PENDING.value,
    WorkflowStatusString.DELAYED.value,
)


def _active_workflows(queues: list[str] | None) -> list[Any]:
    """The workflows waiting or running right now — i.e. the backlog being measured."""
    return get_client().list_workflows(
        status=list(ACTIVE_STATUSES),
        queue_name=queues,
        load_input=False,
        load_output=False,
    )


def _recovery_exceeded_count() -> int:
    dead_lettered = get_client().list_workflows(
        status=WorkflowStatusString.MAX_RECOVERY_ATTEMPTS_EXCEEDED.value, load_input=False, load_output=False
    )
    return len(dead_lettered)


def _queue_capacities() -> dict[str, dict[str, Any]]:
    capacities: dict[str, dict[str, Any]] = {}
    for queue in get_client().list_queues():
        capacities[queue.name] = {
            'worker_concurrency': queue.worker_concurrency,
            'global_concurrency': queue.concurrency,
        }
    return capacities


def collect(queues: list[str] | None = None, *, advisor: Advisor | None = None) -> dict[str, Any]:
    """Collect worker capacity metrics from the DBOS system database.

    All declared queues (see ``settings.resolve_queues``) always appear in the
    report, even when idle.

    Args:
        queues: Restrict the report to these queue names.
        advisor: Overrides the configured advice function for this call.

    Returns:
        A dict with ``queues`` (per-queue load: backlog, waiting age, running,
            declared concurrency, thresholds, advice), ``executors``
            (per-executor running and enqueued counts), ``workers`` (the
            worker heartbeats, stale ones included) and
            ``max_recovery_attempts_exceeded`` (the number of workflows that
            exhausted their recovery attempts).
    """
    capacity_config = resolve_capacity()
    advise: Advisor = advisor or capacity_config.advisor or default_advisor
    declared = {queue.name: queue for queue in resolve_queues()}
    if queues is not None:
        declared = {name: queue for name, queue in declared.items() if name in queues}

    def _empty_queue_metrics() -> dict[str, Any]:
        return {
            'enqueued': 0,
            'delayed': 0,
            'running': 0,
            'backlog_age_seconds': None,
            'max_queue_wait_ms': None,
            'worker_concurrency': None,
            'global_concurrency': None,
            'thresholds': {},
            'advice': 'OK',
        }

    queues_metrics: dict[str, dict[str, Any]] = {name: _empty_queue_metrics() for name in declared}
    capacities = _queue_capacities()

    executors: dict[str, dict[str, int]] = {}
    now = datetime.now(tz=UTC)

    for status in _active_workflows(queues):
        # Child workflows started inline by a parent (cron firings run the
        # task workflow that way) carry no queue: they count for their
        # executor's load, not for any queue's backlog.
        queue_metrics = (
            queues_metrics.setdefault(status.queue_name, _empty_queue_metrics()) if status.queue_name else None
        )
        _ingest_workflow(status, queue_metrics, executors, now)
    for queue_name, queue_metrics in queues_metrics.items():
        for key in ('worker_concurrency', 'global_concurrency'):
            queue_metrics[key] = capacities.get(queue_name, {}).get(key)
        thresholds = capacity_config.thresholds_for(declared.get(queue_name))
        queue_metrics['thresholds'] = thresholds
        queue_metrics['advice'] = advise(queue_name, queue_metrics, thresholds)

    # Stale heartbeats are reported too: a worker that died without cleaning
    # up is exactly what an operator wants to see.
    workers = read_heartbeats(include_stale=True)
    executor_ids = set(executors) | {worker['executor_id'] for worker in workers}
    executors_report = {
        executor_id: executors.get(executor_id, {'running': 0, 'enqueued': 0}) for executor_id in executor_ids
    }
    return {
        'queues': queues_metrics,
        'executors': executors_report,
        'workers': workers,
        'max_recovery_attempts_exceeded': _recovery_exceeded_count(),
    }


def _ingest_workflow(
    status: Any,
    queue_metrics: dict[str, Any] | None,
    executors: dict[str, dict[str, int]],
    now: datetime,
) -> None:
    """Fold one active workflow into its queue's metrics (if any) and its executor's counts."""
    workflow_status = str(getattr(status.status, 'value', status.status))

    if queue_metrics is None:
        pass
    elif workflow_status == WorkflowStatusString.ENQUEUED.value:
        queue_metrics['enqueued'] += 1
        if status.created_at is not None:
            age = (now - datetime.fromtimestamp(status.created_at / 1000, tz=UTC)).total_seconds()
            backlog_age = queue_metrics['backlog_age_seconds']
            if backlog_age is None or age > backlog_age:
                queue_metrics['backlog_age_seconds'] = age
    elif workflow_status == WorkflowStatusString.DELAYED.value:
        queue_metrics['delayed'] += 1
    elif workflow_status == WorkflowStatusString.PENDING.value:
        queue_metrics['running'] += 1
        if status.dequeued_at is not None and status.created_at is not None:
            queue_wait_ms = status.dequeued_at - status.created_at
            queue_metrics['max_queue_wait_ms'] = max(queue_metrics['max_queue_wait_ms'] or 0, queue_wait_ms)

    if (
        workflow_status in (WorkflowStatusString.ENQUEUED.value, WorkflowStatusString.PENDING.value)
        and status.executor_id
    ):
        executor = executors.setdefault(status.executor_id, {'running': 0, 'enqueued': 0})
        executor['running' if workflow_status == WorkflowStatusString.PENDING.value else 'enqueued'] += 1


def advise_queue(
    queue_metrics: dict[str, Any],
    *,
    scale_up_backlog_age_seconds: float | None = None,
    scale_down_max_running: int | None = None,
) -> str:
    """Simple heuristic per queue: SCALE_UP / SCALE_DOWN / OK.

    - SCALE_UP: the oldest workflow waiting on this queue has been waiting
      longer than ``scale_up_backlog_age_seconds`` — the queue is not drained
      fast enough.
    - SCALE_DOWN: nothing waiting and at most ``scale_down_max_running``
      workflows running — an idle queue.
    - OK otherwise.

    Args:
        queue_metrics: One queue's metrics, as reported by ``collect``.
        scale_up_backlog_age_seconds: Defaults to ``OPTIONS["CAPACITY"]``,
            then to ``CAPACITY_THRESHOLD_DEFAULTS``.
        scale_down_max_running: Defaults like ``scale_up_backlog_age_seconds``.

    Returns:
        ``"SCALE_UP"``, ``"SCALE_DOWN"`` or ``"OK"``.
    """
    if scale_up_backlog_age_seconds is None or scale_down_max_running is None:
        configured = resolve_capacity().thresholds
        if scale_up_backlog_age_seconds is None:
            scale_up_backlog_age_seconds = configured['SCALE_UP_BACKLOG_AGE_SECONDS']
        if scale_down_max_running is None:
            scale_down_max_running = int(configured['SCALE_DOWN_MAX_RUNNING'])

    backlog = queue_metrics['enqueued']
    backlog_age = queue_metrics['backlog_age_seconds']
    running = queue_metrics['running']

    if backlog == 0 and running <= scale_down_max_running:
        return 'SCALE_DOWN'
    if backlog > 0 and backlog_age is not None and backlog_age > scale_up_backlog_age_seconds:
        return 'SCALE_UP'
    return 'OK'


def default_advisor(queue_name: str, metrics: dict[str, Any], thresholds: dict[str, float | int]) -> str:
    """The built-in advisor: ``advise_queue`` with the queue's resolved thresholds."""
    return advise_queue(
        metrics,
        scale_up_backlog_age_seconds=thresholds['scale_up_backlog_age_seconds'],
        scale_down_max_running=int(thresholds['scale_down_max_running']),
    )
