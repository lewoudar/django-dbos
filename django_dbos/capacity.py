"""Worker capacity diagnostics for django-dbos.

Everything here comes from the DBOS system database: queue backlogs, waiting
times and per-executor load — plus worker heartbeats pushed by the workers
themselves through Django's cache (see ``heartbeat.py``).

The heuristics are deliberately simple (see ``advise_queue``); for real
capacity planning, expose these numbers to a monitoring system (Prometheus,
etc.).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from dbos import DBOS

from .dbos_setup import ensure_launched
from .heartbeat import read_heartbeats

#: A backlog older than this (its oldest enqueued workflow) means the queue is
#: not drained fast enough: scale up.
QUEUE_WAIT_THRESHOLD_SECONDS = 60.0

#: A queue with no backlog and nothing running is an idle candidate: scale down.
IDLE_RUNNING_THRESHOLD = 0

ACTIVE_STATUSES = ('ENQUEUED', 'PENDING', 'DELAYED', 'RUNNING')


def _sys_db() -> Any:
    """The DBOS SystemDatabase (launched).

    Accesses the private SDK instance because the workflow aggregates are not
    (yet) exposed on the public ``DBOS`` class.
    """
    ensure_launched()
    from dbos._dbos import _get_dbos_instance

    return _get_dbos_instance()._sys_db


def _active_status_aggregates() -> list[dict[str, Any]]:
    sys_db = _sys_db()
    return sys_db.get_workflow_aggregates(
        group_by_status=True,
        group_by_queue_name=True,
        group_by_executor_id=True,
        select_count=True,
        select_min_created_at=True,
        select_max_queue_wait_ms=True,
        status=list(ACTIVE_STATUSES),
    )


def _recovery_exceeded_count() -> int:
    sys_db = _sys_db()
    rows = sys_db.get_workflow_aggregates(
        group_by_status=True,
        select_count=True,
        status=['MAX_RECOVERY_ATTEMPTS_EXCEEDED'],
    )
    return sum(int(row['count'] or 0) for row in rows)


def _queue_capacities() -> dict[str, dict[str, Any]]:
    capacities: dict[str, dict[str, Any]] = {}
    for queue in DBOS.list_queues():
        capacities[queue.name] = {
            'worker_concurrency': queue.worker_concurrency,
            'global_concurrency': queue.concurrency,
        }
    return capacities


def collect(queues: list[str] | None = None) -> dict[str, Any]:
    """Collect worker capacity metrics from the DBOS system database.

    Returns a dict with per-queue load (backlog, waiting age, running, declared
    concurrency, advice), per-executor running counts and the number of
    workflows that exhausted their recovery attempts. Pass ``queues`` to
    restrict the report to specific queue names. All declared queues (see
    ``DJANGO_DBOS["QUEUES"]``) always appear in the report, even when idle.
    """
    from .settings import resolve_queues

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
            'advice': 'OK',
        }

    queues_metrics: dict[str, dict[str, Any]] = {name: _empty_queue_metrics() for name in declared}
    capacities = _queue_capacities()

    executors: dict[str, dict[str, int]] = {}
    now = datetime.now(tz=UTC)

    for row in _active_status_aggregates():
        group = row['group']
        queue_name = group.get('queue_name')
        if queues is not None and queue_name not in queues:
            continue
        queue_metrics = queues_metrics.setdefault(queue_name or '', _empty_queue_metrics())
        for key in ('worker_concurrency', 'global_concurrency'):
            queue_metrics[key] = capacities.get(queue_name, {}).get(key)
        _ingest_aggregate_row(row, queue_metrics, executors, now)
        queue_metrics['advice'] = advise_queue(queue_metrics)

    executor_ids = set(executors) | _recent_executor_ids()
    # Executors that ran recently but have nothing active right now still
    # belong to the report: their missing heartbeat is the signal.
    executors_report = {
        executor_id: executors.get(executor_id, {'running': 0, 'enqueued': 0}) for executor_id in executor_ids
    }
    return {
        'queues': queues_metrics,
        'executors': executors_report,
        'worker_heartbeats': read_heartbeats(executor_ids),
        'max_recovery_attempts_exceeded': _recovery_exceeded_count(),
    }


def _ingest_aggregate_row(
    row: dict[str, Any],
    queue_metrics: dict[str, Any],
    executors: dict[str, dict[str, int]],
    now: datetime,
) -> None:
    """Fold one aggregate row (queue x status x executor) into the report."""
    group = row['group']
    status = group.get('status')
    executor_id = group.get('executor_id')
    count = int(row['count'] or 0)

    if status in ('ENQUEUED', 'PENDING'):
        queue_metrics['enqueued'] += count
        min_created_at = row.get('min_created_at')
        if min_created_at is not None:
            age = (now - datetime.fromtimestamp(min_created_at / 1000, tz=UTC)).total_seconds()
            backlog_age = queue_metrics['backlog_age_seconds']
            if backlog_age is None or age > backlog_age:
                queue_metrics['backlog_age_seconds'] = age
    elif status == 'DELAYED':
        queue_metrics['delayed'] += count
    elif status == 'RUNNING':
        queue_metrics['running'] += count
        if row.get('max_queue_wait_ms') is not None:
            queue_metrics['max_queue_wait_ms'] = max(queue_metrics['max_queue_wait_ms'] or 0, row['max_queue_wait_ms'])

    if status in ('ENQUEUED', 'PENDING', 'RUNNING') and executor_id:
        executor = executors.setdefault(executor_id, {'running': 0, 'enqueued': 0})
        executor['running' if status == 'RUNNING' else 'enqueued'] += count


def advise_queue(queue_metrics: dict[str, Any]) -> str:
    """Simple heuristic per queue: SCALE_UP / SCALE_DOWN / OK.

    - SCALE_UP: the oldest workflow waiting on this queue has been waiting
      longer than ``QUEUE_WAIT_THRESHOLD_SECONDS`` — the queue is not drained
      fast enough.
    - SCALE_DOWN: nothing running and nothing waiting — an idle queue.
    - OK otherwise.
    """
    backlog = queue_metrics['enqueued']
    backlog_age = queue_metrics['backlog_age_seconds']
    running = queue_metrics['running']

    if backlog == 0 and running <= IDLE_RUNNING_THRESHOLD:
        return 'SCALE_DOWN'
    if backlog > 0 and backlog_age is not None and backlog_age > QUEUE_WAIT_THRESHOLD_SECONDS:
        return 'SCALE_UP'
    return 'OK'


def _recent_executor_ids(max_age_seconds: float = 3600.0) -> set[str]:
    """Executor ids that ran a workflow recently (they should have a heartbeat)."""
    cutoff = datetime.fromtimestamp(datetime.now(tz=UTC).timestamp() - max_age_seconds, tz=UTC)
    rows = _sys_db().get_workflow_aggregates(
        group_by_executor_id=True,
        select_count=True,
        # start_time filters on created_at and expects an ISO datetime string
        start_time=cutoff.isoformat(),
    )
    return {row['group'].get('executor_id') or '' for row in rows}
