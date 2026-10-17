"""Management of the DBOS singleton in a worker process.

DBOS uses a process-wide singleton (``dbos.DBOS``). This module wraps its
creation, launch and teardown so the worker entry points (the ``dbos worker``
command, the ``dbos_system`` test fixture) share one idempotent lifecycle:

- ``ensure_initialized`` builds the singleton from the DBOS backend's
  ``OPTIONS``.
- ``ensure_launched`` registers every known task as a DBOS workflow,
  starts the queue/scheduler/recovery threads and declares the queues (DBOS
  persists queue configuration to the system database, which only exists once
  launched): from then on this process executes tasks.
- ``shutdown`` tears the singleton down (used on worker shutdown and
  between tests), waiting for in-flight workflows to finish first.

Processes that only enqueue tasks or inspect the system database (web,
admin, ``dbos status``) never call these: they use ``client.get_client()``.
"""

from __future__ import annotations

import threading
from typing import Any

from dbos import DBOS

from . import settings as settings_module
from . import system_engine
from .exceptions import ImproperlyConfiguredDbos, describe_error
from .scheduling import register_cron_workflows
from .tasks_registry import register_all_task_workflows

_instance_lock = threading.Lock()
_initialized = False
_launched = False
# What system_engine built for the singleton (a CockroachDB engine): DBOS never disposes of it.
_engine_options: dict[str, Any] = {}


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
    # False: launch verifies the schema instead of creating/migrating it
    # (migrate out of band with `manage.py dbos migrate`).
    config['run_migrations'] = settings_module.get_bool_option('RUN_MIGRATIONS')
    scheduler_polling_interval = settings_module.get_dbos_setting('SCHEDULER_POLLING_INTERVAL_SEC')
    if scheduler_polling_interval is not None:
        config['scheduler_polling_interval_sec'] = float(scheduler_polling_interval)
    return config


def _dispose_engine() -> None:
    global _engine_options
    system_engine.dispose(_engine_options)
    _engine_options = {}


def _register_queues() -> None:
    """Declare configured queues, persisting their configuration to the system database.

    Called after ``DBOS.launch()``: ``DBOS.register_queue`` writes through to
    the system database, which only exists once launched.
    """
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
        for name in settings_module.PARTITION_OPTIONS:
            if getattr(queue, name) is not None:
                kwargs[name] = getattr(queue, name)
        DBOS.register_queue(queue.name, **kwargs)


def ensure_initialized() -> None:
    """Create the DBOS singleton if it does not exist yet."""
    global _initialized, _engine_options
    with _instance_lock:
        if _initialized:
            return
        try:
            config = _build_dbos_config()
            _engine_options = system_engine.system_database_options(config['system_database_url'])
            DBOS(config={**config, **_engine_options})  # type: ignore[arg-type]
        except ImproperlyConfiguredDbos:
            _dispose_engine()
            raise
        except Exception as exc:  # noqa: BLE001 - deliberately unchained: the cause would print the DSN
            _dispose_engine()
            # The SDK and SQLAlchemy echo the database URL in their messages:
            # re-raise without the chain, which would print it unmasked.
            raise ImproperlyConfiguredDbos(describe_error(exc)) from None
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
        register_all_task_workflows()
        register_cron_workflows()
        try:
            DBOS.launch()
            # Queues persist to the system database (DBOS.register_queue), which
            # only exists once launched: declare them now, not in ensure_initialized.
            _register_queues()
        except ImproperlyConfiguredDbos:
            raise
        except Exception as exc:  # noqa: BLE001 - deliberately unchained: the cause would print the DSN
            raise ImproperlyConfiguredDbos(describe_error(exc)) from None
        _launched = True


def shutdown(*, workflow_completion_timeout_sec: int = 10) -> None:
    """Destroy the DBOS singleton, if it was created by this module.

    Waits up to ``workflow_completion_timeout_sec`` for workflows already
    running (an enqueued task picked up by a worker thread, a cron firing) to
    finish before disconnecting from the system database. ``DBOS.destroy()``
    defaults to 0 (no wait), which races the SDK's own background threads: a
    step or workflow still persisting its result accesses ``DBOS._sys_db``
    after it has been torn down and raises ``System database accessed before
    DBOS was launched``.
    """
    global _initialized, _launched
    if not _initialized:
        return
    DBOS.destroy(workflow_completion_timeout_sec=workflow_completion_timeout_sec)
    _dispose_engine()
    _initialized = False
    _launched = False
