"""Fixtures for the integration test suite (dogfoods the pytest plugin)."""

from __future__ import annotations

import uuid

import pytest
from django.tasks import TaskResult, TaskResultStatus

from django_dbos.pytest_plugin import dbos_system

# The integration suite runs against the same fixture the plugin ships:
# PostgreSQL via testcontainers when Docker is available, SQLite file otherwise.
dbos_environment = dbos_system


def wait_for_result(result: TaskResult, status: TaskResultStatus, timeout: float = 30.0) -> TaskResult:
    """Poll the backend until the result reaches the expected status."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result.refresh()
        if result.status == status:
            return result
        time.sleep(0.2)
    raise AssertionError(f"Task did not reach {status} within {timeout}s (status: {result.status})")


@pytest.fixture()
def unique_queue_name() -> str:
    return f"queue-{uuid.uuid4().hex[:8]}"
