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

from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from dbos import DBOS, StepOptions, WorkflowHandle

from .exceptions import DjangoDbosError


class NoActiveDbosContext(DjangoDbosError):
    """get_dbos_context() was called outside a DBOS workflow execution."""


def _step_options(name: str | None, step_options: StepOptions | None) -> StepOptions:
    options = StepOptions(**step_options) if step_options else StepOptions()
    if name:
        options['name'] = name
    return options


@dataclass(frozen=True)
class DbosTaskContext:
    """Durable primitives available inside a task with ``durable_operations``."""

    workflow_id: str

    def run_step(
        self,
        func: Any,
        *args: Any,
        name: str | None = None,
        step_options: StepOptions | None = None,
        **kwargs: Any,
    ) -> Any:
        """Execute ``func`` as a checkpointed step of the enclosing workflow.

        Args:
            func: The function to run as a step.
            *args: Positional arguments passed to ``func``.
            name: Step name recorded by DBOS; defaults to the function's name.
                Takes precedence over a ``name`` in ``step_options``.
            step_options: DBOS step options, such as ``retries_allowed``,
                ``max_attempts``, ``interval_seconds`` and ``backoff_rate``;
                without them, the step is not retried.
            **kwargs: Keyword arguments passed to ``func``.

        Returns:
            The result of ``func``, replayed from the checkpoint on recovery.
        """
        return DBOS.run_step(_step_options(name, step_options), func, *args, **kwargs)

    async def run_step_async(
        self,
        func: Any,
        *args: Any,
        name: str | None = None,
        step_options: StepOptions | None = None,
        **kwargs: Any,
    ) -> Any:
        """Async variant of ``run_step``."""
        return await DBOS.run_step_async(_step_options(name, step_options), func, *args, **kwargs)

    def sleep(self, seconds: float) -> None:
        """Sleep durably: the workflow stops and resumes after the delay."""
        DBOS.sleep(seconds)

    async def sleep_async(self, seconds: float) -> None:
        """Async variant of ``sleep``."""
        await DBOS.sleep_async(seconds)

    def set_event(self, key: str, value: Any) -> None:
        """Publish an event on the current workflow, for external consumers."""
        DBOS.set_event(key, value)

    async def set_event_async(self, key: str, value: Any) -> None:
        """Async variant of ``set_event``."""
        await DBOS.set_event_async(key, value)

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

    async def get_event_async(self, workflow_id: str, key: str, timeout_seconds: float = 60) -> Any:
        """Async variant of ``get_event``."""
        return await DBOS.get_event_async(workflow_id, key, timeout_seconds)

    def get_all_events(self, workflow_id: str) -> dict[str, Any]:
        """Read every event currently set on a workflow, without waiting.

        Args:
            workflow_id: The workflow whose events are read.

        Returns:
            The events, keyed by event key.
        """
        return DBOS.get_all_events(workflow_id)

    async def get_all_events_async(self, workflow_id: str) -> dict[str, Any]:
        """Async variant of ``get_all_events``."""
        return await DBOS.get_all_events_async(workflow_id)

    def send(self, destination_workflow_id: str, message: Any, topic: str | None = None) -> None:
        """Send a durable message to another workflow.

        Args:
            destination_workflow_id: The receiving workflow.
            message: The message payload.
            topic: Optional topic, matched by the receiver's ``recv``.
        """
        DBOS.send(destination_workflow_id, message, topic)

    async def send_async(self, destination_workflow_id: str, message: Any, topic: str | None = None) -> None:
        """Async variant of ``send``."""
        await DBOS.send_async(destination_workflow_id, message, topic)

    def recv(self, topic: str | None = None, timeout_seconds: float = 60) -> Any:
        """Wait for a durable message sent to this workflow.

        Args:
            topic: Only receive messages sent on this topic.
            timeout_seconds: How long to wait before giving up.

        Returns:
            The message, or ``None`` on timeout.
        """
        return DBOS.recv(topic, timeout_seconds)

    async def recv_async(self, topic: str | None = None, timeout_seconds: float = 60) -> Any:
        """Async variant of ``recv``."""
        return await DBOS.recv_async(topic, timeout_seconds)

    def write_stream(self, key: str, value: Any) -> None:
        """Append a value to a stream of the current workflow, for external readers.

        Args:
            key: The stream key.
            value: The value to append.
        """
        DBOS.write_stream(key, value)

    async def write_stream_async(self, key: str, value: Any) -> None:
        """Async variant of ``write_stream``."""
        await DBOS.write_stream_async(key, value)

    def close_stream(self, key: str) -> None:
        """Close a stream of the current workflow: its readers stop after the last value.

        Args:
            key: The stream key.
        """
        DBOS.close_stream(key)

    async def close_stream_async(self, key: str) -> None:
        """Async variant of ``close_stream``."""
        await DBOS.close_stream_async(key)

    def read_stream(
        self,
        workflow_id: str,
        key: str,
        *,
        offset: int = 0,
        timeout_seconds: float | None = None,
    ) -> Iterator[Any]:
        """Read the values of another workflow's stream, in order.

        Args:
            workflow_id: The workflow that writes the stream.
            key: The stream key.
            offset: Index of the first value to read.
            timeout_seconds: How long to wait for each value; waits forever
                when ``None``.

        Yields:
            Each value, until the stream is closed or its workflow ends.

        Raises:
            dbos.error.DBOSStreamTimeoutError: No value arrived within
                ``timeout_seconds``.
        """
        yield from DBOS.read_stream(workflow_id, key, offset=offset, timeout_seconds=timeout_seconds)

    async def read_stream_async(
        self,
        workflow_id: str,
        key: str,
        *,
        offset: int = 0,
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[Any]:
        """Async variant of ``read_stream``."""
        async for value in DBOS.read_stream_async(workflow_id, key, offset=offset, timeout_seconds=timeout_seconds):
            yield value

    def read_stream_offset(
        self,
        workflow_id: str,
        key: str,
        offset: int,
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        """Read the single value at one offset of another workflow's stream, waiting for it.

        Args:
            workflow_id: The workflow that writes the stream.
            key: The stream key.
            offset: Index of the value to read.
            timeout_seconds: How long to wait for it; waits forever when ``None``.

        Returns:
            The value at ``offset``.

        Raises:
            dbos.error.DBOSStreamTimeoutError: The value did not arrive within
                ``timeout_seconds``, or the stream ended before ``offset``.
        """
        return DBOS.read_stream_offset(workflow_id, key, offset, timeout_seconds=timeout_seconds)

    async def read_stream_offset_async(
        self,
        workflow_id: str,
        key: str,
        offset: int,
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        """Async variant of ``read_stream_offset``."""
        return await DBOS.read_stream_offset_async(workflow_id, key, offset, timeout_seconds=timeout_seconds)

    def start_child_workflow(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """Start a child workflow without waiting for it."""
        return DBOS.start_workflow(func, *args, **kwargs)

    def wait_first(
        self, handles: list[WorkflowHandle[Any]], *, polling_interval_sec: float | None = None
    ) -> WorkflowHandle[Any]:
        """Wait for the first of several workflows to finish.

        The wait is checkpointed: on replay, the same handle is returned.

        Args:
            handles: Handles of the workflows to wait for, e.g. from
                ``start_child_workflow``.
            polling_interval_sec: How often to check the workflows' status,
                in seconds; DBOS' default when ``None``.

        Returns:
            The handle of a workflow that has finished, successfully or not.

        Raises:
            ValueError: ``handles`` is empty or holds the same workflow twice.
        """
        if polling_interval_sec is None:
            return DBOS.wait_first(handles)
        return DBOS.wait_first(handles, polling_interval_sec=polling_interval_sec)


def as_completed(
    handles: Iterable[WorkflowHandle[Any]], *, polling_interval_sec: float | None = None
) -> Iterator[WorkflowHandle[Any]]:
    """Iterate over workflows as they finish, from inside a task with ``durable_operations``.

    A loop over ``DbosTaskContext.wait_first``: each wait is checkpointed, so
    a replay goes through the handles in the same order.

    Args:
        handles: Handles of the workflows to wait for, e.g. from
            ``start_child_workflow``.
        polling_interval_sec: How often to check the workflows' status, in
            seconds; DBOS' default when ``None``.

    Yields:
        Each handle once its workflow has finished, successfully or not:
        ``handle.get_result()`` returns its result or raises its error.

    Raises:
        NoActiveDbosContext: Called outside a task with ``durable_operations``.
        ValueError: ``handles`` holds the same workflow twice.
    """
    ctx = get_dbos_context()
    pending = list(handles)
    while pending:
        handle = ctx.wait_first(pending, polling_interval_sec=polling_interval_sec)
        pending.remove(handle)
        yield handle


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
