"""Backend-specific task options for django-dbos.

``@dbos_params`` attaches DBOS-specific execution options to a Django task.
Options are stored on the task function itself, so they survive pickling and
``Task.using()``; on non-django-dbos backends they are simply inert.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .params_dataclass import DbosTaskParams

FUNC_PARAMS_ATTRIBUTE = "_django_dbos_params"
_UNSET = object()


def dbos_params(
    _task: Any = _UNSET,
    *,
    max_recovery_attempts: int | None = None,
    step_retries_allowed: bool = False,
    step_max_attempts: int = 3,
    step_interval_seconds: float = 1.0,
    step_backoff_rate: float = 2.0,
    deduplication_id: str | None = None,
    queue_partition_key: str | None = None,
    workflow_timeout: float | None = None,
    durable_operations: bool = False,
) -> Any:
    """Attach DBOS execution options to a Django task.

    Works both below and above ``@task``::

        @dbos_params(step_retries_allowed=True, step_max_attempts=5)
        @task
        def my_task(): ...
    """
    params = DbosTaskParams(
        max_recovery_attempts=max_recovery_attempts,
        step_retries_allowed=step_retries_allowed,
        step_max_attempts=step_max_attempts,
        step_interval_seconds=step_interval_seconds,
        step_backoff_rate=step_backoff_rate,
        deduplication_id=deduplication_id,
        queue_partition_key=queue_partition_key,
        workflow_timeout=workflow_timeout,
        durable_operations=durable_operations,
    )

    def wrapper(task: Any) -> Any:
        from .tasks import DBOSTask

        if isinstance(task, DBOSTask):
            return replace(task, dbos_params=params)
        # Bare function, or a Task from another backend: keep the params on
        # the function; DBOSTask picks them up when the DBOS backend is used.
        setattr(task.func if hasattr(task, "func") else task, FUNC_PARAMS_ATTRIBUTE, params)
        return task

    if _task is not _UNSET:
        # Used as @dbos_params without parentheses
        return wrapper(_task)
    return wrapper
