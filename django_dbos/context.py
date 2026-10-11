"""Durable DBOS primitives available inside a running task.

By default, a task's function runs as a single checkpointed DBOS step: simple,
and retried atomically, but it cannot itself use DBOS durable primitives
(steps, durable sleep, events, messages are workflow-level operations).

Tasks that need them opt in with ``@dbos_params(durable_operations=True)``;
their function then runs at workflow level and can call
``get_dbos_context`` to checkpoint work explicitly:

    from django.tasks import task
    from django_dbos import dbos_params, get_dbos_context

    @dbos_params(durable_operations=True)
    @task
    def onboard_user(user_id: int) -> None:
        ctx = get_dbos_context()
        ctx.run_step(send_welcome_email, user_id)
        ctx.sleep(86400)  # one day, durably
        ctx.run_step(send_followup_email, user_id)

``run_step`` is for plain functions (third-party code, or a name computed per
call). A function you own can instead be decorated with DBOS's own
``@DBOS.step`` and called directly with its normal arguments: the workflow-level
context is already active, so the call is checkpointed the same way, with no
need to go through ``ctx.run_step``.
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
        """Execute ``func`` as a checkpointed step of the enclosing workflow.

        Args:
            func: The function to run as a step.
            *args: Positional arguments passed to ``func``.
            name: Step name recorded by DBOS; defaults to the function's name.
            **kwargs: Keyword arguments passed to ``func``.

        Returns:
            The result of ``func``, replayed from the checkpoint on recovery.
        """
        options = StepOptions(name=name) if name else StepOptions()
        return DBOS.run_step(options, func, *args, **kwargs)

    async def run_step_async(self, func: Any, *args: Any, name: str | None = None, **kwargs: Any) -> Any:
        """Async variant of ``run_step``."""
        options = StepOptions(name=name) if name else StepOptions()
        return await DBOS.run_step_async(options, func, *args, **kwargs)

    def sleep(self, seconds: float) -> None:
        """Sleep durably: the workflow stops and resumes after the delay."""
        DBOS.sleep(seconds)

    async def sleep_async(self, seconds: float) -> None:
        """Async variant of ``sleep``."""
        await DBOS.sleep_async(seconds)

    def set_event(self, key: str, value: Any) -> None:
        """Publish an event on the current workflow, for external consumers."""
        DBOS.set_event(key, value)

    def get_event(self, workflow_id: str, key: str, timeout_seconds: float = 60) -> Any:
        """Wait for an event set by another workflow.

        Args:
            workflow_id: The workflow that publishes the event.
            key: The event key.
            timeout_seconds: How long to wait before giving up.

        Returns:
            The event value, or ``None`` on timeout.
        """
        return DBOS.get_event(workflow_id, key, timeout_seconds)

    def send(self, destination_workflow_id: str, message: Any, topic: str | None = None) -> None:
        """Send a durable message to another workflow.

        Args:
            destination_workflow_id: The receiving workflow.
            message: The message payload.
            topic: Optional topic, matched by the receiver's ``recv``.
        """
        DBOS.send(destination_workflow_id, message, topic)

    def recv(self, topic: str | None = None, timeout_seconds: float = 60) -> Any:
        """Wait for a durable message sent to this workflow.

        Args:
            topic: Only receive messages sent on this topic.
            timeout_seconds: How long to wait before giving up.

        Returns:
            The message, or ``None`` on timeout.
        """
        return DBOS.recv(topic, timeout_seconds)

    def start_child_workflow(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """Start a child workflow without waiting for it."""
        return DBOS.start_workflow(func, *args, **kwargs)


def get_dbos_context() -> DbosTaskContext:
    """Access durable DBOS primitives from inside a running task.

    Requires the task to be declared with ``@dbos_params(durable_operations=True)``
    and to be executing inside the DBOS worker.

    Returns:
        The context of the running task's workflow.

    Raises:
        NoActiveDbosContext: Called outside a running task, or from a step
            (a task without ``durable_operations``, a function run by
            ``run_step``, or one decorated with ``@DBOS.step``).
    """
    workflow_id = DBOS.workflow_id
    if not workflow_id:
        raise NoActiveDbosContext(
            'get_dbos_context() must be called from within a running task '
            'declared with @dbos_params(durable_operations=True).'
        )
    if DBOS.step_id is not None:
        # Without durable_operations the task body itself runs as a step:
        # DBOS' workflow-level primitives would fail later with a bare
        # AssertionError ("sleep() must be called from within a workflow").
        raise NoActiveDbosContext(
            'get_dbos_context() was called from inside a step: declare the task with '
            '@dbos_params(durable_operations=True), and do not call it from a function '
            'run by run_step() or decorated with @DBOS.step.'
        )
    return DbosTaskContext(workflow_id=workflow_id)
