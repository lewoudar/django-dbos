"""Management of the DBOS singleton from a Django process.

DBOS uses a process-wide singleton (``dbos.DBOS``). This module wraps its
creation, launch and teardown so Django entry points (``AppConfig.ready()``,
management commands, tests) can share one idempotent lifecycle:

- :func:`ensure_initialized` builds the singleton from the ``DJANGO_DBOS``
  setting. It does not start background workers: enqueuing tasks is possible
  right after, the tasks just wait in the system database.
- :func:`ensure_launched` additionally starts queue/scheduler threads, i.e.
  everything a worker process needs.
- :func:`shutdown` tears the singleton down (used on worker shutdown and
  between tests).
"""

from __future__ import annotations

import threading
from typing import Any

from dbos import DBOS

from . import settings as settings_module
from .exceptions import ImproperlyConfiguredDbos

_instance_lock = threading.Lock()
_initialized = False
_launched = False


def _build_dbos_config() -> dict[str, Any]:
    resolved = settings_module.resolve_settings()
    config: dict[str, Any] = {
        "name": resolved.app_name,
        "system_database_url": resolved.system_database_url,
    }
    if resolved.application_version:
        config["application_version"] = resolved.application_version
    if resolved.dbos_system_schema:
        config["dbos_system_schema"] = resolved.dbos_system_schema
    scheduler_polling_interval = settings_module.get_dbos_setting("SCHEDULER_POLLING_INTERVAL_SEC")
    if scheduler_polling_interval is not None:
        config["scheduler_polling_interval_sec"] = float(scheduler_polling_interval)
    return config


def _register_queues() -> None:
    """Declare configured queues in the DBOS registry.

    Queues are plain (non database-backed) objects here: their configuration is
    pushed to the system database at launch time.
    """
    from dbos import Queue

    for queue in settings_module.resolve_queues():
        kwargs: dict[str, Any] = {}
        if queue.worker_concurrency is not None:
            kwargs["worker_concurrency"] = queue.worker_concurrency
        if queue.global_concurrency is not None:
            kwargs["global_concurrency"] = queue.global_concurrency
        if queue.limiter is not None:
            kwargs["limiter"] = queue.limiter
        if queue.polling_interval_sec is not None:
            kwargs["polling_interval_sec"] = queue.polling_interval_sec
        Queue(queue.name, **kwargs)


def ensure_initialized() -> None:
    """Create the DBOS singleton if it does not exist yet."""
    global _initialized
    with _instance_lock:
        if _initialized:
            return
        try:
            config = _build_dbos_config()
        except Exception as exc:
            raise ImproperlyConfiguredDbos(str(exc)) from exc
        DBOS(config=config)  # type: ignore[arg-type]
        _register_queues()
        _initialized = True


def ensure_launched() -> None:
    """Ensure the DBOS singleton exists and its background services run."""
    global _launched
    ensure_initialized()
    with _instance_lock:
        if _launched:
            return
        DBOS.launch()
        _launched = True


def shutdown() -> None:
    """Destroy the DBOS singleton, if it was created by this module."""
    global _initialized, _launched
    if not _initialized:
        return
    DBOS.destroy()
    _initialized = False
    _launched = False
