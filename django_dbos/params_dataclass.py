"""The DbosTaskParams dataclass, kept free of Django imports."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class DbosTaskParams:
    """DBOS execution options for a task.

    Attributes:
        max_recovery_attempts: How many times DBOS recovers a workflow that
            crashed mid-execution before giving up.
        step_retries_allowed: Whether the task function (run as a DBOS step)
            is retried on failure.
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
    """

    max_recovery_attempts: int | None = None
    step_retries_allowed: bool = False
    step_max_attempts: int = 3
    step_interval_seconds: float = 1.0
    step_backoff_rate: float = 2.0
    deduplication_id: str | None = None
    workflow_timeout: float | None = None
    durable_operations: bool = False
