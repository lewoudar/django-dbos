"""The DbosTaskParams dataclass, kept free of Django imports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Attribute of a task function holding the DbosTaskParams set by ``@dbos_params``.
FUNC_PARAMS_ATTRIBUTE = '_django_dbos_params'


@dataclass(frozen=True, kw_only=True)
class DbosTaskParams:
    """DBOS execution options for a task.

    Attributes:
        max_recovery_attempts: How many times DBOS recovers a workflow that
            crashed mid-execution before giving up.
        step_retries_allowed: Whether the task function (run as a DBOS step)
            is retried on failure. The ``step_*`` options are ignored with
            ``durable_operations``, where the function is not a step.
        step_max_attempts: Max attempts for the task function when step
            retries are enabled.
        step_interval_seconds: Delay between step retries.
        step_backoff_rate: Multiplier applied to ``step_interval_seconds``
            after each failed attempt.
        deduplication_id: Prevents duplicate enqueues on a queue: while a
            workflow with this ID is enqueued or running, further enqueues
            raise an error.
        workflow_timeout: Max wall-clock duration of the whole workflow, in
            seconds.
        durable_operations: Run the task function at workflow level instead of
            as a single checkpointed step, enabling durable primitives inside
            the task (``get_dbos_context()``: additional steps, durable sleep,
            events, messages). The task is then responsible for checkpointing
            its side effects.
        partition_key: Partition of the enqueued execution on a partitioned
            queue (one declaring a ``partition_*`` limit): the name of a task
            argument, or a callable receiving the task's arguments, whose value
            (as a string) is the key. Ignored on other queues.
    """

    max_recovery_attempts: int | None = None
    step_retries_allowed: bool = False
    step_max_attempts: int = 3
    step_interval_seconds: float = 1.0
    step_backoff_rate: float = 2.0
    deduplication_id: str | None = None
    workflow_timeout: float | None = None
    durable_operations: bool = False
    partition_key: str | Callable[..., Any] | None = None
