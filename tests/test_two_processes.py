"""End-to-end test of the production topology: enqueue here, execute in another process.

This process plays the web application: it never initializes nor launches
DBOS, it enqueues through the ``DBOSClient`` and polls the result. A
subprocess plays ``dbos worker`` (autodiscovery + launch) on its own SQLite
system database.
"""

from __future__ import annotations

import time

import pytest
from django.tasks import TaskResultStatus

from django_dbos import client

from .helpers import run_worker
from .tasks import add

pytestmark = pytest.mark.integration


@pytest.fixture
def worker_process(tmp_path, monkeypatch):
    """A dbos worker-like subprocess on a fresh SQLite system database."""
    system_database_url = f'sqlite:///{tmp_path / "systemdb.sqlite"}'
    # This process (the "web" side) targets the same system database through
    # a fresh client, and touches nothing DBOS-related besides the client.
    monkeypatch.setenv('DBOS_SYSTEM_DATABASE_URL', system_database_url)
    client.close_client()
    with run_worker(system_database_url) as process:
        yield process
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
