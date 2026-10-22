"""Helpers shared by the test suite."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sentry_sdk.transport import Transport

from django_dbos.tasks import DBOSTask
from django_dbos.workflow_names import workflow_name_for_task

from .tasks import add


def noop() -> None: ...


def set_dbos_options(settings: Any, options: dict[str, Any]) -> None:
    """Replace the ``OPTIONS`` of the test DBOS backend through pytest-django's ``settings`` fixture."""
    settings.TASKS = {**settings.TASKS, 'dbos': {**settings.TASKS['dbos'], 'OPTIONS': options}}


REPO_ROOT = Path(__file__).resolve().parent.parent

WORKER_SCRIPT = """
import time

import django

django.setup()

from dbos import DBOS

from django_dbos import dbos_setup, discovery

discovery.autodiscover_tasks()
dbos_setup.ensure_launched()
print('WORKER_READY', flush=True)

deadline = time.monotonic() + 60
while time.monotonic() < deadline and not DBOS.list_workflows(status='SUCCESS'):
    time.sleep(0.2)
dbos_setup.shutdown()
"""


@contextmanager
def run_worker(system_database_url: str, *, startup_timeout: float = 60) -> Iterator[subprocess.Popen[str]]:
    """A dbos worker-like subprocess on ``system_database_url``, until a workflow succeeds (60 s at most)."""
    env = {
        **os.environ,
        'DBOS_SYSTEM_DATABASE_URL': system_database_url,
        'DJANGO_SETTINGS_MODULE': 'tests.settings',
        'PYTHONPATH': str(REPO_ROOT),
        'PYTHONUNBUFFERED': '1',
    }
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter and script
        [sys.executable, '-c', WORKER_SCRIPT],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert process.stdout is not None
    lines: list[str] = []
    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if not line:
            break
        lines.append(line)
        if 'WORKER_READY' in line:
            break
    else:
        process.kill()
        raise AssertionError('worker did not start:\n' + ''.join(lines))

    try:
        yield process
        process.wait(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
        process.stdout.close()


def make_dbos_task(**overrides: Any) -> DBOSTask:
    """Build a DBOSTask from a module-level test function."""
    return DBOSTask(func=noop, **overrides)


class MemoryTransport(Transport):
    """In-memory Sentry transport collecting events and envelopes."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.envelopes: list[Any] = []

    def capture_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def capture_envelope(self, envelope: Any) -> None:
        self.envelopes.append(envelope)


WORKFLOW_ID = 'wf-1'
CREATED_AT_MS = 1_700_000_000_000


def make_status(**overrides: Any) -> SimpleNamespace:
    """A DBOS workflow status of the ``add`` task, successful unless ``overrides`` say otherwise."""
    values = {
        'status': 'SUCCESS',
        'name': workflow_name_for_task(add),
        'error': None,
        'input': {'args': [1, 2], 'kwargs': {}},
        'output': 3,
        'created_at': CREATED_AT_MS,
        'updated_at': CREATED_AT_MS + 1_000,
        'dequeued_at': CREATED_AT_MS + 200,
        'completed_at': CREATED_AT_MS + 1_000,
        'workflow_timeout_ms': None,
        'workflow_deadline_epoch_ms': None,
        'recovery_attempts': 1,
        'executor_id': 'worker-1',
    }
    return SimpleNamespace(**(values | overrides))


def ms(value: datetime | None) -> int | None:
    """A result timestamp as epoch milliseconds, to compare with DBOS' values."""
    return None if value is None else round(value.timestamp() * 1000)
