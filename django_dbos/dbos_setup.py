"""Management of the DBOS singleton in a worker process.

DBOS uses a process-wide singleton (``dbos.DBOS``). This module wraps its
creation, launch and teardown so the worker entry points (the ``dbos_worker``
command, the ``dbos_system`` test fixture) share one idempotent lifecycle:

- :func:`ensure_initialized` builds the singleton from the ``DJANGO_DBOS``
  setting and declares the queues.
- :func:`ensure_launched` registers every known task as a DBOS workflow and
  starts the queue/scheduler/recovery threads: from then on this process
  executes tasks.
- :func:`shutdown` tears the singleton down (used on worker shutdown and
  between tests).

Processes that only enqueue tasks or inspect the system database (web,
admin, ``dbos_status``) never call these: they use ``client.get_client()``.
"""

from __future__ import annotations

import re
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
        'name': resolved.app_name,
        'system_database_url': resolved.system_database_url,
    }
    if resolved.application_version:
        config['application_version'] = resolved.application_version
    if resolved.dbos_system_schema:
        config['dbos_system_schema'] = resolved.dbos_system_schema
    if resolved.executor_id:
        config['executor_id'] = resolved.executor_id
    if resolved.serializer is not None:
        config['serializer'] = resolved.serializer
    scheduler_polling_interval = settings_module.get_dbos_setting('SCHEDULER_POLLING_INTERVAL_SEC')
    if scheduler_polling_interval is not None:
        config['scheduler_polling_interval_sec'] = float(scheduler_polling_interval)
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
            kwargs['worker_concurrency'] = queue.worker_concurrency
        if queue.global_concurrency is not None:
            kwargs['global_concurrency'] = queue.global_concurrency
        if queue.limiter is not None:
            kwargs['limiter'] = queue.limiter
        if queue.polling_interval_sec is not None:
            kwargs['polling_interval_sec'] = queue.polling_interval_sec
        Queue(queue.name, **kwargs)


#: A DSN with credentials embedded in an error message, e.g.
#: ``postgresql+psycopg://user:secrets@host/db``. Driver and ORMs may include
#: one in connection errors; the password is masked before showing the message.
_DSN_WITH_PASSWORD_RE = re.compile(r"([a-z][a-z0-9+.-]*://[^\s:/@\"']+):([^\s@\"'/]+)@", re.IGNORECASE)


def _scrub_credentials(message: str) -> str:
    """Mask passwords in any DSN found in an error message."""
    return _DSN_WITH_PASSWORD_RE.sub(r'\1:***@', message)


def ensure_initialized() -> None:
    """Create the DBOS singleton if it does not exist yet."""
    global _initialized
    with _instance_lock:
        if _initialized:
            return
        try:
            config = _build_dbos_config()
        except Exception as exc:
            raise ImproperlyConfiguredDbos(_scrub_credentials(str(exc))) from exc
        DBOS(config=config)  # type: ignore[arg-type]
        _register_queues()
        _initialized = True


def ensure_launched() -> None:
    """Ensure the DBOS singleton exists and its background services run.

    Every task declared so far is registered as a workflow first: once
    launched, this process dequeues workflows enqueued by any process and can
    only execute the ones it knows.
    """
    global _launched
    ensure_initialized()
    with _instance_lock:
        if _launched:
            return
        from .tasks_registry import register_all_task_workflows

        register_all_task_workflows()
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
