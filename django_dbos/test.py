"""Helpers for testing applications that use django-dbos."""

from __future__ import annotations

from typing import Any

from django.tasks import Task


def run_task(task: Task, *args: Any, **kwargs: Any) -> Any:
    """Execute a task function synchronously in-process, bypassing the backend.

    Useful in tests to exercise the task body without a DBOS system database.

    Args:
        task: The task to run.
        *args: Positional arguments passed to the task function.
        **kwargs: Keyword arguments passed to the task function.

    Returns:
        Whatever the task function returns.

    Example:
        ```python
        def test_send_email():
            result = run_task(send_email, "kevin@example.com")
            assert result == "sent to kevin@example.com"
        ```
    """
    return task.call(*args, **kwargs)


async def arun_task(task: Task, *args: Any, **kwargs: Any) -> Any:
    """Async variant of ``run_task``."""
    return await task.acall(*args, **kwargs)
