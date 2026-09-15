"""Registration of Django tasks as DBOS workflows.

Each Django task function is wrapped into a DBOS workflow whose body runs the
task function as a single checkpointed step. This gives DBOS-level durability
(the enqueue survives crashes) plus optional step retries, while keeping the
user-facing API exactly Django's ``@task``.

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

from dbos import DBOS, StepOptions
from dbos._dbos import _get_or_create_dbos_registry

from .logging import logger
from .params_dataclass import DbosTaskParams
from .tasks import DBOSTask, known_tasks

REGISTRY_PREFIX = 'django_dbos.tasks.'

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
    """Build a Django ``TaskContext`` for a currently-running task."""
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


def register_task_workflow(task: DBOSTask) -> str:
    """Register (or reuse) the DBOS workflow executing this task.

    Returns the workflow name to enqueue.
    """
    name = workflow_name_for_task(task)
    if name in _get_or_create_dbos_registry().workflow_info_map:
        return name

    options = _step_options(task.dbos_params)
    takes_context = task.takes_context
    max_recovery_attempts = task.dbos_params.max_recovery_attempts
    durable_operations = task.dbos_params.durable_operations
    func = task.func

    if inspect.iscoroutinefunction(func):

        @DBOS.workflow(name=name, max_recovery_attempts=max_recovery_attempts)
        async def workflow(*args: Any, **kwargs: Any) -> Any:
            if takes_context:
                args = (_build_task_context(task, args, kwargs), *args)
            if durable_operations:
                # Runs at workflow level: the task uses get_dbos_context() to
                # checkpoint its own operations.
                return await func(*args, **kwargs)
            return await DBOS.run_step_async(options, func, *args, **kwargs)

    else:

        @DBOS.workflow(name=name, max_recovery_attempts=max_recovery_attempts)
        def workflow(*args: Any, **kwargs: Any) -> Any:
            if takes_context:
                args = (_build_task_context(task, args, kwargs), *args)
            if durable_operations:
                return func(*args, **kwargs)
            return DBOS.run_step(options, func, *args, **kwargs)

    logger.debug('registered DBOS workflow', workflow_name=name)
    return name


def register_all_task_workflows() -> None:
    """Register a DBOS workflow for every task declared in this process.

    Called by the worker after task autodiscovery: a workflow enqueued by
    another process can only be executed here if its function is registered.
    """
    for task in known_tasks():
        register_task_workflow(task)
