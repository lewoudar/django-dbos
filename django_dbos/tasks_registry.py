"""Registration of Django tasks as DBOS workflows.

Each Django task function is wrapped into a DBOS workflow whose body runs the
task function as a single checkpointed step. This gives DBOS-level durability
(the enqueue survives crashes) plus optional step retries, while keeping the
user-facing API exactly Django's ``@task``.

The task function runs in DBOS' long-lived executor threads, outside any
request cycle: Django's ORM connections are recycled around each execution
(``close_old_connections``), exactly as a request would, so ``CONN_MAX_AGE``
is honoured and a connection dropped by the database is not reused.

Workflows are named after the task function's module path, which lets
:meth:`DBOSBackend.get_result` find the original task back from a workflow
status. Only the worker registers them (:func:`register_all_task_workflows`,
before ``DBOS.launch()``): other processes enqueue by name through
``DBOSClient`` and never hold workflow code.
"""

from __future__ import annotations

import inspect
import re
from datetime import UTC
from typing import Any

from asgiref.sync import sync_to_async
from dbos import DBOS, StepOptions
from django.db import close_old_connections

from .exceptions import describe_error
from .logging import logger
from .params_dataclass import DbosTaskParams
from .tasks import DBOSTask, known_tasks

REGISTRY_PREFIX = 'django_dbos.tasks.'

#: Workflow functions registered with DBOS by this process, by workflow name
#: (task workflows and cron parents alike). ``DBOS.workflow`` returns the
#: decorated function; keeping it here avoids the SDK's private registry and
#: makes registration idempotent.
_workflows: dict[str, Any] = {}

#: Task module paths resolved via ``import_string`` come from a name stored in
#: the system database (workflow status); reject anything that is not a plain
#: dotted module path before importing it.
_MODULE_PATH_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$')


def workflow_name_for_task(task: DBOSTask) -> str:
    """The DBOS workflow name for a task: its Django module path."""
    return f'{REGISTRY_PREFIX}{task.module_path}'


def task_from_workflow_name(workflow_name: str) -> Any:
    """Resolve a registered workflow name back to its DBOSTask instance."""
    from django.utils.module_loading import import_string

    if not workflow_name.startswith(REGISTRY_PREFIX):
        return None
    module_path = workflow_name.removeprefix(REGISTRY_PREFIX)
    if not _MODULE_PATH_RE.fullmatch(module_path):
        return None
    try:
        task = import_string(module_path)
    except (ImportError, AttributeError):
        return None
    # Only actual tasks are usable as results; the name comes from the system
    # database, so anything else (arbitrary module attributes, ...) is rejected.
    return task if isinstance(task, DBOSTask) else None


def _step_options(params: DbosTaskParams) -> StepOptions:
    return StepOptions(
        name='task_body',
        retries_allowed=params.step_retries_allowed,
        max_attempts=params.step_max_attempts,
        interval_seconds=params.step_interval_seconds,
        backoff_rate=params.step_backoff_rate,
    )


def _build_task_context(task: DBOSTask, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """Build a Django ``TaskContext`` for a currently-running task.

    Its ``task_result`` is read from the system database, so ``attempt``
    counts this task's executions (crash recoveries included). The read goes
    through the client, not ``DBOS.get_workflow_status``: inside a workflow,
    the latter is checkpointed and would replay a stale count after a crash.
    """
    from .backends import _build_task_result
    from .client import get_client

    workflow_id = DBOS.workflow_id or ''
    try:
        status = get_client().retrieve_workflow(workflow_id).get_status()
        return _context_for(_build_task_result(workflow_id, status))
    except Exception as exc:  # noqa: BLE001 - a failed read must not fail the task itself
        logger.warning('could not read the task status, context.attempt defaults to 1', error=describe_error(exc))
        return _fallback_task_context(task, args, kwargs)


async def _abuild_task_context(task: DBOSTask, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """Async variant of :func:`_build_task_context` (the sync client read refuses a running event loop)."""
    from .backends import _build_task_result
    from .client import get_client

    workflow_id = DBOS.workflow_id or ''
    try:
        handle = await get_client().retrieve_workflow_async(workflow_id)
        status = await handle.get_status()
        return _context_for(_build_task_result(workflow_id, status))
    except Exception as exc:  # noqa: BLE001 - a failed read must not fail the task itself
        logger.warning('could not read the task status, context.attempt defaults to 1', error=describe_error(exc))
        return _fallback_task_context(task, args, kwargs)


def _context_for(result: Any) -> Any:
    from django.tasks import TaskContext

    return TaskContext(task_result=result)


def _fallback_task_context(task: DBOSTask, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """A context built without the system database: a single attempt, timestamps of now."""
    from datetime import datetime

    from django.tasks import TaskContext, TaskResult, TaskResultStatus

    workflow_id = DBOS.workflow_id or ''
    now = datetime.now(tz=UTC)
    result = TaskResult(
        task=task,
        id=workflow_id,
        status=TaskResultStatus.RUNNING,
        enqueued_at=now,
        started_at=now,
        finished_at=None,
        last_attempted_at=now,
        args=list(args),
        kwargs=dict(kwargs),
        backend=task.backend,
        errors=[],
        worker_ids=[''],
    )
    return TaskContext(task_result=result)


def _call_with_fresh_connections(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Run ``func`` with Django connections recycled before and after."""
    close_old_connections()
    try:
        return func(*args, **kwargs)
    finally:
        close_old_connections()


async def _acall_with_fresh_connections(func: Any, *args: Any, **kwargs: Any) -> Any:
    # Same thread-sensitive bridge Django's ASGI handler uses.
    aclose_old_connections = sync_to_async(close_old_connections, thread_sensitive=True)
    await aclose_old_connections()
    try:
        return await func(*args, **kwargs)
    finally:
        await aclose_old_connections()


def execute_task_body(task: DBOSTask, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """Run a (sync) task function inside the current DBOS workflow.

    Prepends the ``TaskContext`` when the task takes one, then runs the
    function either as a single checkpointed step (default) or at workflow
    level (``durable_operations``: the task checkpoints its own operations
    through ``get_dbos_context()``).
    """
    if task.takes_context:
        args = (_build_task_context(task, args, kwargs), *args)
    if task.dbos_params.durable_operations:
        return _call_with_fresh_connections(task.func, *args, **kwargs)
    return DBOS.run_step(_step_options(task.dbos_params), _call_with_fresh_connections, task.func, *args, **kwargs)


async def aexecute_task_body(task: DBOSTask, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """Async variant of :func:`execute_task_body`."""
    if task.takes_context:
        args = (await _abuild_task_context(task, args, kwargs), *args)
    if task.dbos_params.durable_operations:
        return await _acall_with_fresh_connections(task.func, *args, **kwargs)
    return await DBOS.run_step_async(
        _step_options(task.dbos_params), _acall_with_fresh_connections, task.func, *args, **kwargs
    )


def register_task_workflow(task: DBOSTask) -> str:
    """Register (or reuse) the DBOS workflow executing this task.

    Returns the workflow name to enqueue.
    """
    name = workflow_name_for_task(task)
    if name in _workflows:
        return name

    max_recovery_attempts = task.dbos_params.max_recovery_attempts

    if inspect.iscoroutinefunction(task.func):

        @DBOS.workflow(name=name, max_recovery_attempts=max_recovery_attempts)
        async def workflow(*args: Any, **kwargs: Any) -> Any:
            return await aexecute_task_body(task, args, kwargs)

    else:

        @DBOS.workflow(name=name, max_recovery_attempts=max_recovery_attempts)
        def workflow(*args: Any, **kwargs: Any) -> Any:
            return execute_task_body(task, args, kwargs)

    _workflows[name] = workflow
    logger.debug('registered DBOS workflow', workflow_name=name)
    return name


def task_workflow(task: DBOSTask) -> Any:
    """The registered DBOS workflow function executing ``task`` (registering it if needed)."""
    return _workflows[register_task_workflow(task)]


def registered_workflow(name: str) -> Any | None:
    """The workflow function registered under ``name`` by this process, if any."""
    return _workflows.get(name)


def remember_workflow(name: str, workflow: Any) -> Any:
    """Record a workflow function registered with DBOS under ``name``."""
    _workflows[name] = workflow
    return workflow


def register_all_task_workflows() -> None:
    """Register a DBOS workflow for every task declared in this process.

    Called by the worker after task autodiscovery: a workflow enqueued by
    another process can only be executed here if its function is registered.
    """
    for task in known_tasks():
        register_task_workflow(task)
