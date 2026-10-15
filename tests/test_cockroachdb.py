"""End-to-end test on a CockroachDB system database (testcontainers).

``manage.py migrate`` only creates the database there; the worker, launched
in a subprocess, creates DBOS' tables, then executes a task this process (the
web side) enqueues through the client.
"""

from __future__ import annotations

import time

import pytest
import sqlalchemy as sa
from django.tasks import TaskResultStatus
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from django_dbos import client, system_db

from .tasks import add
from .tests_helpers import run_worker

pytestmark = pytest.mark.integration

COCKROACHDB_IMAGE = 'cockroachdb/cockroach:v25.2.4'
SQL_PORT = 26257
DATABASE = 'dbos_django_test'


@pytest.fixture(scope='module')
def cockroachdb_server():
    """``cockroachdb+psycopg2://root@host:port`` of an insecure single-node CockroachDB."""
    container = (
        DockerContainer(COCKROACHDB_IMAGE)
        .with_exposed_ports(SQL_PORT)
        .with_command('start-single-node --insecure')
        # Printed once the node serves SQL connections.
        .waiting_for(LogMessageWaitStrategy('nodeID:'))
    )
    try:
        container.start()
    except Exception as exc:  # noqa: BLE001 - Docker/testcontainers errors are not typed
        pytest.skip(f'Docker/CockroachDB unavailable: {exc}')
    yield f'cockroachdb+psycopg2://root@{container.get_container_host_ip()}:{container.get_exposed_port(SQL_PORT)}'
    container.stop()


@pytest.fixture
def system_database_url(cockroachdb_server, monkeypatch):
    url = f'{cockroachdb_server}/{DATABASE}?sslmode=disable'
    monkeypatch.setenv('DBOS_SYSTEM_DATABASE_URL', url)
    client.close_client()
    yield url
    client.close_client()


def database_exists(server: str) -> bool:
    engine = sa.create_engine(f'{server}/defaultdb?sslmode=disable')
    try:
        with engine.connect() as conn:
            return DATABASE in conn.execute(sa.text('SELECT database_name FROM [SHOW DATABASES]')).scalars().all()
    finally:
        engine.dispose()


def test_migrate_then_worker_execute_a_task(cockroachdb_server, system_database_url):
    messages: list[str] = []
    system_db.migrate_system_database(echo=messages.append)
    system_db.migrate_system_database(echo=messages.append)  # idempotent
    assert database_exists(cockroachdb_server)
    assert 'The dbos worker creates the DBOS tables in it when it launches.' in messages

    # DBOS' migrations run on the worker's launch: allow for them.
    with run_worker(system_database_url, startup_timeout=240) as worker:
        result = add.enqueue(20, 22)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            result.refresh()
            if result.status == TaskResultStatus.SUCCESSFUL:
                break
            time.sleep(0.2)
        assert result.status == TaskResultStatus.SUCCESSFUL, worker.stdout.read()
        assert result.return_value == 42
