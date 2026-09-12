"""Fixtures for the integration test suite."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from django.tasks import TaskResult, TaskResultStatus

postgres_url: str | None = None


@pytest.fixture(scope="session")
def dbos_environment(tmp_path_factory) -> Iterator[None]:
    """Launch DBOS on a system database for the whole session.

    Uses a Postgres container when Docker is available (that is the production
    setup), and falls back to a file-based SQLite system database otherwise —
    the backend logic under test is the same.
    """
    global postgres_url

    from django_dbos import dbos_setup

    try:
        from testcontainers.community.postgres import PostgresContainer

        container: PostgresContainer | None = PostgresContainer("postgres:17")
        container.start()
        postgres_url = container.get_connection_url().replace("postgresql+psycopg2://", "postgresql+psycopg://")
    except Exception as exc:  # noqa: BLE001 - docker may be unavailable
        container = None
        sqlite_file = tmp_path_factory.mktemp("dbos") / "systemdb.sqlite"
        postgres_url = f"sqlite:///{sqlite_file}"
        print(f"\nPostgres unavailable ({exc}); using SQLite system database: {sqlite_file}")

    os.environ["DBOS_SYSTEM_DATABASE_URL"] = postgres_url

    dbos_setup.ensure_launched()
    yield
    dbos_setup.shutdown()
    del os.environ["DBOS_SYSTEM_DATABASE_URL"]
    if container is not None:
        container.stop()
    postgres_url = None


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
