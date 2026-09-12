"""Durable DBOS primitives available inside a running task.

By default a task's function runs as a single checkpointed DBOS step: simple,
and retried atomically, but it cannot itself use DBOS durable primitives
(steps, durable sleep, events, messages are workflow-level operations).

Tasks that need them opt in with ``@dbos_params(durable_operations=True)``;
their function then runs at workflow level and can call
:func:`get_dbos_context` to checkpoint work explicitly::

    from django.tasks import task
    from django_dbos import dbos_params, get_dbos_context

    @dbos_params(durable_operations=True)
    @task
    def onboard_user(user_id: int) -> None:
        ctx = get_dbos_context()
        ctx.run_step(send_welcome_email, user_id)
        ctx.sleep(86400)  # one day, durably
        ctx.run_step(send_followup_email, user_id)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dbos import DBOS, StepOptions

from .exceptions import DjangoDbosError


class NoActiveDbosContext(DjangoDbosError):
    """get_dbos_context() was called outside a DBOS workflow execution."""


@dataclass(frozen=True)
class DbosTaskContext:
    """Durable primitives available inside a task with ``durable_operations``."""

    workflow_id: str

    def run_step(self, func: Any, *args: Any, name: str | None = None, **kwargs: Any) -> Any:
        """Execute ``func`` as a checkpointed step of the enclosing workflow."""
        options = StepOptions(name=name) if name else StepOptions()
        return DBOS.run_step(options, func, *args, **kwargs)

    async def run_step_async(self, func: Any, *args: Any, name: str | None = None, **kwargs: Any) -> Any:
        """Async variant of :meth:`run_step`."""
        options = StepOptions(name=name) if name else StepOptions()
        return await DBOS.run_step_async(options, func, *args, **kwargs)

    def sleep(self, seconds: float) -> None:
        """Sleep durably: the workflow stops and resumes after the delay."""
        DBOS.sleep(seconds)

    async def sleep_async(self, seconds: float) -> None:
        await DBOS.sleep_async(seconds)

    def set_event(self, key: str, value: Any) -> None:
        """Publish an event on the current workflow, for external consumers."""
        DBOS.set_event(key, value)

    def get_event(self, workflow_id: str, key: str, timeout_seconds: float = 60) -> Any:
        """Wait for an event set by another workflow."""
        return DBOS.get_event(workflow_id, key, timeout_seconds)

    def send(self, destination_workflow_id: str, message: Any, topic: str | None = None) -> None:
        """Send a durable message to another workflow."""
        DBOS.send(destination_workflow_id, message, topic)

    def recv(self, topic: str | None = None, timeout_seconds: float = 60) -> Any:
        """Wait for a durable message sent to this workflow."""
        return DBOS.recv(topic, timeout_seconds)

    def start_child_workflow(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """Start a child workflow without waiting for it."""
        return DBOS.start_workflow(func, *args, **kwargs)


def get_dbos_context() -> DbosTaskContext:
    """Access durable DBOS primitives from inside a running task.

    Requires the task to be declared with ``@dbos_params(durable_operations=True)``
    and to be executing inside the DBOS worker.
    """
    workflow_id = DBOS.workflow_id
    if not workflow_id:
        raise NoActiveDbosContext(
            "get_dbos_context() must be called from within a running task "
            "declared with @dbos_params(durable_operations=True)."
        )
    return DbosTaskContext(workflow_id=workflow_id)
