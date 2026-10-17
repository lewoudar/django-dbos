"""Backend-specific task options for django-dbos.

``@dbos_params`` attaches DBOS-specific execution options to a Django task.
Options are stored on the task function itself, so they survive pickling and
``Task.using()``; on non-django-dbos backends they are simply inert.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from .exceptions import ImproperlyConfiguredDbos
from .params_dataclass import FUNC_PARAMS_ATTRIBUTE, DbosTaskParams
from .tasks import DBOSTask

_UNSET = object()


def enqueue_signature(task: DBOSTask) -> inspect.Signature:
    """The signature of the arguments passed to ``task.enqueue()``.

    Args:
        task: The task whose function is inspected.

    Returns:
        The function's signature, without the context parameter of a task
        declared with ``takes_context=True`` (Django passes it, not the caller).
    """
    signature = inspect.signature(task.func)
    if not task.takes_context:
        return signature
    return signature.replace(parameters=list(signature.parameters.values())[1:])


def resolve_partition_key(task: DBOSTask, args: Any, kwargs: Any) -> str | None:
    """The partition key of an enqueue, from the task's ``partition_key`` option.

    Args:
        task: The enqueued task.
        args: The positional arguments of the enqueue.
        kwargs: The keyword arguments of the enqueue.

    Returns:
        The key as a string, or ``None`` when the task has no ``partition_key``.

    Raises:
        ImproperlyConfiguredDbos: The key is ``None`` or empty, or the
            arguments do not match the task's signature.
    """
    partition_key = task.dbos_params.partition_key
    if partition_key is None:
        return None

    if callable(partition_key):
        value = partition_key(*args, **kwargs)
    else:
        try:
            bound = enqueue_signature(task).bind(*args, **kwargs)
        except TypeError as exc:
            raise ImproperlyConfiguredDbos(f"Cannot read the partition key of task '{task.name}': {exc}") from exc
        bound.apply_defaults()
        value = bound.arguments[partition_key]

    key = '' if value is None else str(value)
    if not key:
        raise ImproperlyConfiguredDbos(f"The partition key of task '{task.name}' is empty (got {value!r}).")
    return key


def dbos_params(
    _task: Any = _UNSET,
    *,
    max_recovery_attempts: int | None = None,
    step_retries_allowed: bool = False,
    step_max_attempts: int = 3,
    step_interval_seconds: float = 1.0,
    step_backoff_rate: float = 2.0,
    deduplication_id: str | None = None,
    workflow_timeout: float | None = None,
    durable_operations: bool = False,
    partition_key: str | Callable[..., Any] | None = None,
) -> Any:
    """Attach DBOS execution options to a Django task.

    Works both below and above ``@task``, with or without parentheses. The
    options are documented on ``DbosTaskParams``, whose fields they set.

    Args:
        _task: The decorated task or function, when used without parentheses.
        max_recovery_attempts: See ``DbosTaskParams``.
        step_retries_allowed: See ``DbosTaskParams``.
        step_max_attempts: See ``DbosTaskParams``.
        step_interval_seconds: See ``DbosTaskParams``.
        step_backoff_rate: See ``DbosTaskParams``.
        deduplication_id: See ``DbosTaskParams``.
        workflow_timeout: See ``DbosTaskParams``.
        durable_operations: See ``DbosTaskParams``.
        partition_key: See ``DbosTaskParams``.

    Returns:
        The decorated task when ``_task`` is given, otherwise a decorator.

    Raises:
        ImproperlyConfiguredDbos: ``partition_key`` is neither a string nor a
            callable.

    Example:
        ```python
        @dbos_params(step_retries_allowed=True, step_max_attempts=5)
        @task
        def my_task(): ...
        ```
    """
    if partition_key is not None and not (isinstance(partition_key, str) or callable(partition_key)):
        raise ImproperlyConfiguredDbos(
            f'partition_key must be the name of a task argument or a callable, got {partition_key!r}.'
        )
    params = DbosTaskParams(
        max_recovery_attempts=max_recovery_attempts,
        step_retries_allowed=step_retries_allowed,
        step_max_attempts=step_max_attempts,
        step_interval_seconds=step_interval_seconds,
        step_backoff_rate=step_backoff_rate,
        deduplication_id=deduplication_id,
        workflow_timeout=workflow_timeout,
        durable_operations=durable_operations,
        partition_key=partition_key,
    )

    def wrapper(task: Any) -> Any:
        if isinstance(task, DBOSTask):
            return replace(task, dbos_params=params)
        # Bare function, or a Task from another backend: keep the params on
        # the function; DBOSTask picks them up when the DBOS backend is used.
        setattr(task.func if hasattr(task, 'func') else task, FUNC_PARAMS_ATTRIBUTE, params)
        return task

    if _task is not _UNSET:
        # Used as @dbos_params without parentheses
        return wrapper(_task)
    return wrapper
