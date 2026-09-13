"""Worker heartbeats: live worker metrics through Django's cache framework.

Workers periodically push a heartbeat (process metrics via psutil when the
``[metrics]`` extra is installed) to a cache key keyed by executor id. Diagnostics
(the ``dbos_status`` command, the admin) read these keys back.

This mirrors Conductor's push model, using Django's cache as the channel: with
a shared cache (Redis, db) the heartbeats are visible from any process; with
``LocMemCache`` each worker only sees its own (the section then degrades
gracefully). The cache TTL makes dead workers disappear by itself — a missing
heartbeat means a stale worker.
"""

from __future__ import annotations

import threading
from typing import Any

from django.core.cache import cache

from .logging import logger

HEARTBEAT_INTERVAL_SECONDS = 10.0
HEARTBEAT_TTL_SECONDS = 30.0
HEARTBEAT_KEY_PREFIX = "django_dbos:worker_heartbeat:"

_sentinel = object()


def _cache_key(executor_id: str) -> str:
    return f"{HEARTBEAT_KEY_PREFIX}{executor_id}"


def _local_metrics() -> dict[str, Any] | None:
    """Local process metrics via psutil, or None when the extra is missing."""
    try:
        import psutil
    except ImportError:
        return None

    process = psutil.Process()
    return {
        "cpu_percent": process.cpu_percent(interval=None),
        "memory_rss_mb": round(process.memory_info().rss / 1024 / 1024, 1),
        "pid": process.pid,
    }


def write_heartbeat(executor_id: str, has_metrics: bool) -> bool:
    """Push this worker's heartbeat to the shared cache.

    Returns False when the cache is not shared/available (LocMemCache can only
    be read back by the writing process).
    """
    payload: dict[str, Any] = {"executor_id": executor_id, "has_metrics": has_metrics}
    if has_metrics:
        metrics = _local_metrics()
        if metrics is not None:
            payload |= metrics
    try:
        cache.set(_cache_key(executor_id), payload, timeout=HEARTBEAT_TTL_SECONDS)
        return True
    except Exception as exc:  # noqa: BLE001 - a cache failure must not kill the worker
        logger.warning("could not write worker heartbeat", error=str(exc))
        return False


def read_heartbeats(executor_ids: Any) -> dict[str, Any]:
    """Read heartbeats for the given executor ids (missing ones are omitted)."""
    heartbeats: dict[str, Any] = {}
    for executor_id in executor_ids:
        payload = cache.get(_cache_key(executor_id), _sentinel)
        if payload is not _sentinel:
            heartbeats[executor_id] = payload
    return heartbeats


class HeartbeatThread(threading.Thread):
    """Background thread pushing this worker's heartbeat until stopped."""

    def __init__(self, executor_id: str, has_metrics: bool) -> None:
        super().__init__(name="django-dbos-heartbeat", daemon=True)
        self._executor_id = executor_id
        self._has_metrics = has_metrics
        self._stop_event = threading.Event()

    def run(self) -> None:
        logger.info("worker heartbeat thread started", executor_id=self._executor_id)
        while not self._stop_event.wait(timeout=0.0):
            write_heartbeat(self._executor_id, self._has_metrics)
            self._stop_event.wait(timeout=HEARTBEAT_INTERVAL_SECONDS)
        # Final write so the key outlives the worker shutdown log noise.
        write_heartbeat(self._executor_id, self._has_metrics)

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=5.0)
