"""Worker heartbeats: liveness and load of worker processes, in the Django database.

Each ``dbos worker`` periodically upserts a ``WorkerHeartbeat``
row (process metrics via psutil when the ``[metrics]`` extra is installed)
and deletes it on clean shutdown. Diagnostics (``dbos status``, the admin)
read the rows back: a row whose ``last_seen_at`` is older than
``STALE_AFTER_SECONDS`` belongs to a worker that died without cleaning up.

This mirrors DBOS Conductor's push model with the Django database as the
channel — it works wherever the ORM does, from any process, without a
shared cache. Rows are keyed per worker run (a UUID generated when the
heartbeat thread starts), so workers sharing an ``executor_id`` (DBOS'
default ``"local"``) stay apart, and a worker restarted after a crash with
the same hostname and pid gets a fresh row while the old one goes stale.
"""

from __future__ import annotations

import os
import socket
import threading
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, TypedDict

from django.db import DatabaseError, close_old_connections
from django.utils import timezone

from .logging import logger

if TYPE_CHECKING:
    from .models import WorkerHeartbeat

HEARTBEAT_INTERVAL_SECONDS = 10.0
# A heartbeat older than this means the worker died without cleaning up.
STALE_AFTER_SECONDS = 30.0
# Stale rows older than this are purged by the next heartbeat write.
PURGE_AFTER_SECONDS = 24 * 3600.0
# How long stop() waits for the thread to delete its row before giving up.
STOP_TIMEOUT_SECONDS = 5.0


class HeartbeatRow(TypedDict):
    """One worker heartbeat, as read back by ``read_heartbeats``."""

    id: str
    worker_id: str
    executor_id: str
    hostname: str
    pid: int
    queues: list[str]
    has_metrics: bool
    cpu_percent: float | None
    memory_rss_mb: float | None
    started_at: datetime
    last_seen_at: datetime
    alive: bool
    """Whether the heartbeat is fresh (not older than ``STALE_AFTER_SECONDS``)."""


def worker_id(executor_id: str) -> str:
    """Unique identity of this process: ``executor_id@hostname:pid``."""
    return f'{executor_id}@{socket.gethostname()}:{os.getpid()}'


def _local_metrics() -> dict[str, Any] | None:
    """Local process metrics via psutil, or None when the extra is missing."""
    try:
        import psutil
    except ImportError:
        return None

    process = psutil.Process()
    return {
        'cpu_percent': process.cpu_percent(interval=None),
        'memory_rss_mb': round(process.memory_info().rss / 1024 / 1024, 1),
    }


def write_heartbeat(
    executor_id: str,
    *,
    heartbeat_id: uuid.UUID,
    queues: list[str] | tuple[str, ...] = (),
    has_metrics: bool = False,
    started_at: datetime | None = None,
) -> bool:
    """Upsert this worker's heartbeat row. Returns False when the database write failed."""
    from .models import WorkerHeartbeat

    now = timezone.now()
    values: dict[str, Any] = {
        'worker_id': worker_id(executor_id),
        'executor_id': executor_id,
        'hostname': socket.gethostname(),
        'pid': os.getpid(),
        'queues': list(queues),
        'has_metrics': has_metrics,
        'cpu_percent': None,
        'memory_rss_mb': None,
        'last_seen_at': now,
    }
    if has_metrics:
        metrics = _local_metrics()
        if metrics is not None:
            values |= metrics
    try:
        try:
            WorkerHeartbeat.objects.update_or_create(
                id=heartbeat_id,
                defaults=values,
                create_defaults={**values, 'started_at': started_at or now},
            )
        except DatabaseError as exc:
            logger.warning('could not write worker heartbeat', error=str(exc))
            return False
        # Autocommit: the heartbeat is written even if the purge fails.
        try:
            WorkerHeartbeat.objects.filter(last_seen_at__lt=now - timedelta(seconds=PURGE_AFTER_SECONDS)).delete()
        except DatabaseError as exc:
            logger.warning('could not purge stale worker heartbeats', error=str(exc))
        return True
    finally:
        close_old_connections()


def delete_heartbeat(heartbeat_id: uuid.UUID) -> None:
    """Remove this worker's row (clean shutdown)."""
    from .models import WorkerHeartbeat

    try:
        WorkerHeartbeat.objects.filter(id=heartbeat_id).delete()
    except DatabaseError as exc:
        logger.warning('could not delete worker heartbeat', error=str(exc))
    finally:
        close_old_connections()


def _serialize(heartbeat: WorkerHeartbeat, now: datetime) -> HeartbeatRow:
    age = (now - heartbeat.last_seen_at).total_seconds()
    return {
        'id': str(heartbeat.pk),
        'worker_id': heartbeat.worker_id,
        'executor_id': heartbeat.executor_id,
        'hostname': heartbeat.hostname,
        'pid': heartbeat.pid,
        'queues': list(heartbeat.queues or []),
        'has_metrics': heartbeat.has_metrics,
        'cpu_percent': heartbeat.cpu_percent,
        'memory_rss_mb': heartbeat.memory_rss_mb,
        'started_at': heartbeat.started_at,
        'last_seen_at': heartbeat.last_seen_at,
        'alive': age <= STALE_AFTER_SECONDS,
    }


def read_heartbeats(*, include_stale: bool = False) -> list[HeartbeatRow]:
    """Heartbeats of the workers, as plain dicts (alive ones only by default).

    Returns an empty list (with a warning) when the table is missing, i.e.
    the ``django_dbos`` migrations have not been applied yet.
    """
    from .models import WorkerHeartbeat

    now = timezone.now()
    try:
        queryset = WorkerHeartbeat.objects.all()
        if not include_stale:
            queryset = queryset.filter(last_seen_at__gte=now - timedelta(seconds=STALE_AFTER_SECONDS))
        return [_serialize(heartbeat, now) for heartbeat in queryset]
    except DatabaseError as exc:
        logger.warning(
            'could not read worker heartbeats (are the django_dbos migrations applied?)',
            error=str(exc),
        )
        return []


class HeartbeatThread(threading.Thread):
    """Background thread pushing this worker's heartbeat until stopped."""

    def __init__(self, executor_id: str, *, queues: tuple[str, ...] = (), has_metrics: bool = False) -> None:
        from .models import new_heartbeat_id

        super().__init__(name='django-dbos-heartbeat', daemon=True)
        self._heartbeat_id = new_heartbeat_id()
        self._executor_id = executor_id
        self._queues = queues
        self._has_metrics = has_metrics
        self._started_at = timezone.now()
        self._stop_event = threading.Event()

    def run(self) -> None:
        logger.info('worker heartbeat thread started', executor_id=self._executor_id)
        while True:
            write_heartbeat(
                self._executor_id,
                heartbeat_id=self._heartbeat_id,
                queues=self._queues,
                has_metrics=self._has_metrics,
                started_at=self._started_at,
            )
            if self._stop_event.wait(timeout=HEARTBEAT_INTERVAL_SECONDS):
                break
        delete_heartbeat(self._heartbeat_id)

    def stop(self) -> None:
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=STOP_TIMEOUT_SECONDS)
            if self.is_alive():
                # Daemon thread: the process still exits, the row goes stale.
                logger.warning(
                    'worker heartbeat thread did not stop in time, its heartbeat may linger',
                    executor_id=self._executor_id,
                    timeout=STOP_TIMEOUT_SECONDS,
                )
