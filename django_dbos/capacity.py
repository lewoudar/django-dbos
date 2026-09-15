"""Worker capacity diagnostics for django-dbos.

Everything here comes from the DBOS system database, read through the public
``DBOSClient`` API (never by launching DBOS): the *active* workflows — the
backlog being measured — are listed and folded into per-queue and
per-executor figures, plus worker heartbeats pushed by the workers themselves
into the Django database (see ``heartbeat.py``).

The heuristics are deliberately simple (see ``advise_queue``); for real
capacity planning, feed :func:`collect` to a monitoring system (Prometheus,
etc.) or an autoscaling script.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .client import get_client
from .heartbeat import read_heartbeats

#: A backlog older than this (its oldest enqueued workflow) means the queue is
#: not drained fast enough: scale up.
QUEUE_WAIT_THRESHOLD_SECONDS = 60.0

#: A queue with no backlog and nothing running is an idle candidate: scale down.
IDLE_RUNNING_THRESHOLD = 0

ACTIVE_STATUSES = ('ENQUEUED', 'PENDING', 'DELAYED', 'RUNNING')


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
        status='MAX_RECOVERY_ATTEMPTS_EXCEEDED', load_input=False, load_output=False
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


def collect(queues: list[str] | None = None) -> dict[str, Any]:
    """Collect worker capacity metrics from the DBOS system database.

    Returns a dict with per-queue load (backlog, waiting age, running, declared
    concurrency, advice), per-executor running counts, the live worker
    heartbeats and the number of workflows that exhausted their recovery
    attempts. Pass ``queues`` to restrict the report to specific queue names.
    All declared queues (see ``DJANGO_DBOS["QUEUES"]``) always appear in the
    report, even when idle.
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

    for status in _active_workflows(queues):
        queue_name = status.queue_name or ''
        queue_metrics = queues_metrics.setdefault(queue_name, _empty_queue_metrics())
        _ingest_workflow(status, queue_metrics, executors, now)
    for queue_name, queue_metrics in queues_metrics.items():
        for key in ('worker_concurrency', 'global_concurrency'):
            queue_metrics[key] = capacities.get(queue_name, {}).get(key)
        queue_metrics['advice'] = advise_queue(queue_metrics)

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
    queue_metrics: dict[str, Any],
    executors: dict[str, dict[str, int]],
    now: datetime,
) -> None:
    """Fold one active workflow into its queue's metrics and its executor's counts."""
    workflow_status = str(getattr(status.status, 'value', status.status))

    if workflow_status in ('ENQUEUED', 'PENDING'):
        queue_metrics['enqueued'] += 1
        if status.created_at is not None:
            age = (now - datetime.fromtimestamp(status.created_at / 1000, tz=UTC)).total_seconds()
            backlog_age = queue_metrics['backlog_age_seconds']
            if backlog_age is None or age > backlog_age:
                queue_metrics['backlog_age_seconds'] = age
    elif workflow_status == 'DELAYED':
        queue_metrics['delayed'] += 1
    elif workflow_status == 'RUNNING':
        queue_metrics['running'] += 1
        if status.dequeued_at is not None and status.created_at is not None:
            queue_wait_ms = status.dequeued_at - status.created_at
            queue_metrics['max_queue_wait_ms'] = max(queue_metrics['max_queue_wait_ms'] or 0, queue_wait_ms)

    if workflow_status in ('ENQUEUED', 'PENDING', 'RUNNING') and status.executor_id:
        executor = executors.setdefault(status.executor_id, {'running': 0, 'enqueued': 0})
        executor['running' if workflow_status == 'RUNNING' else 'enqueued'] += 1


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
