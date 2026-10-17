"""Fixtures for the integration test suite (dogfoods the pytest plugin)."""

from __future__ import annotations

import time

import pytest
from django.tasks import TaskResult, TaskResultStatus

from django_dbos import tasks as tasks_module
from django_dbos.pytest_plugin import dbos_system

# The integration suite runs against the same fixture the plugin ships:
# PostgreSQL via testcontainers when Docker is available, SQLite file otherwise.
dbos_environment = dbos_system


@pytest.fixture()
def keep_known_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the known tasks registry intact: building a task variant replaces the original there."""
    monkeypatch.setattr(tasks_module, '_known_tasks', dict(tasks_module._known_tasks))


def wait_for_result(result: TaskResult, status: TaskResultStatus, timeout: float = 30.0) -> TaskResult:
    """Poll the backend until the result reaches the expected status."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result.refresh()
        if result.status == status:
            return result
        time.sleep(0.2)
    raise AssertionError(f'Task did not reach {status} within {timeout}s (status: {result.status})')
