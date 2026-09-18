"""End-to-end test of the production topology: enqueue here, execute in another process.

This process plays the web application: it never initializes nor launches
DBOS, it enqueues through the ``DBOSClient`` and polls the result. A
subprocess plays ``dbos worker`` (autodiscovery + launch) on its own SQLite
system database.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from django.tasks import TaskResultStatus

from django_dbos import client

from .tasks import add

pytestmark = pytest.mark.integration

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


@pytest.fixture
def worker_process(tmp_path, monkeypatch):
    """A dbos worker-like subprocess on a fresh SQLite system database."""
    system_database_url = f'sqlite:///{tmp_path / "systemdb.sqlite"}'
    # This process (the "web" side) targets the same system database through
    # a fresh client, and touches nothing DBOS-related besides the client.
    monkeypatch.setenv('DBOS_SYSTEM_DATABASE_URL', system_database_url)
    client.close_client()

    env = {
        **os.environ,
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
    deadline = time.monotonic() + 60
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

    yield process

    try:
        process.wait(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
        process.stdout.close()
        client.close_client()


def test_web_process_enqueues_and_separate_worker_executes(worker_process, mocker):
    ensure_initialized = mocker.patch('django_dbos.dbos_setup.ensure_initialized')
    ensure_launched = mocker.patch('django_dbos.dbos_setup.ensure_launched')

    result = add.enqueue(20, 22)
    assert result.status == TaskResultStatus.READY

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        result.refresh()
        if result.status == TaskResultStatus.SUCCESSFUL:
            break
        time.sleep(0.2)
    assert result.status == TaskResultStatus.SUCCESSFUL, worker_process.stdout.read()
    assert result.return_value == 42

    ensure_initialized.assert_not_called()
    ensure_launched.assert_not_called()
