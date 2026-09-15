"""Pytest plugin for django-dbos.

Distributed through the ``pytest11`` entry point, so it loads automatically
whenever django-dbos is installed — no configuration needed. Everything here
imports Django/DBOS lazily (inside fixtures), because pytest loads plugins
before ``django.setup()`` and even for non-Django test suites.

Fixtures:

- ``dbos_inline``: patch the DBOS backend so enqueued tasks execute
  synchronously in-process, without a DBOS system database. Serves the
  majority of test needs.
- ``dbos_system``: run a real (in-process) DBOS worker on a system database —
  SQLite file by default, a PostgreSQL container when
  ``--dbos-system-db=postgres`` is passed (requires Docker and
  ``testcontainers[postgres]``). Use it when you need real semantics:
  step retries, durable sleeps, schedules. Enqueues go through the same
  ``DBOSClient`` path as production; the launched DBOS executes them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest


def pytest_addoption(parser: Any) -> None:
    group = parser.getgroup('django-dbos')
    group.addoption(
        '--dbos-inline-strict',
        action='store_true',
        default=False,
        help="Make django-dbos' dbos_inline fixture fail on features it cannot "
        'reproduce (deferred tasks, durable operations).',
    )
    group.addoption(
        '--dbos-system-db',
        choices=['auto', 'sqlite', 'postgres'],
        default='auto',
        help="System database used by the dbos_system fixture: 'postgres' "
        "spins a testcontainers container, 'auto' uses it when possible and "
        'falls back to a SQLite file.',
    )


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _inline_result(task: Any, args: tuple[Any, ...], kwargs: dict[str, Any], return_value: Any) -> Any:
    from django.tasks import TaskResult, TaskResultStatus

    now = _now()
    result = TaskResult(
        task=task,
        id=str(uuid.uuid4()),
        status=TaskResultStatus.SUCCESSFUL,
        enqueued_at=now,
        started_at=now,
        finished_at=now,
        last_attempted_at=now,
        args=list(args),
        kwargs=dict(kwargs),
        backend=task.backend,
        errors=[],
        worker_ids=['inline'],
    )
    object.__setattr__(result, '_return_value', return_value)
    return result


def _inline_enqueue(self: Any, task: Any, args: Any, kwargs: Any) -> Any:
    from django_dbos.exceptions import DjangoDbosError

    strict = self._inline_strict
    self.validate_task(task)

    if task.run_after is not None and strict:
        raise DjangoDbosError(
            f"Task '{task.name}' is deferred (run_after) and cannot be "
            'executed inline. Test the task body with run_task() or use '
            'the dbos_system fixture.'
        )

    if task.dbos_params.durable_operations and strict:
        raise DjangoDbosError(
            f"Task '{task.name}' uses durable operations, which the inline "
            'mode cannot reproduce. Use the dbos_system fixture.'
        )

    if task.takes_context:
        from django_dbos.tasks_registry import _build_task_context

        context = _build_task_context(task, args, kwargs)
        args = (context, *args)

    return_value = task.call(*args, **kwargs)
    result = _inline_result(task, args, kwargs, return_value)
    _results[result.id] = result
    return result


_results: dict[str, Any] = {}


def _lookup_result(self: Any, result_id: str) -> Any:
    from django.tasks.exceptions import TaskResultDoesNotExist

    try:
        return _results[result_id]
    except KeyError:
        raise TaskResultDoesNotExist(result_id) from None


@pytest.fixture
def dbos_inline(monkeypatch: Any, request: Any) -> Any:
    """Make DBOS-backed tasks execute synchronously, without a system database."""
    from django_dbos.backends import DBOSBackend

    strict = bool(request.config.getoption('--dbos-inline-strict'))
    monkeypatch.setattr(DBOSBackend, '_inline_strict', strict, raising=False)
    monkeypatch.setattr(DBOSBackend, 'enqueue', _inline_enqueue)
    monkeypatch.setattr(DBOSBackend, 'get_result', _lookup_result)
    _results.clear()

    yield

    _results.clear()


# --- Real DBOS system database -------------------------------------------


@pytest.fixture(scope='session')
def dbos_system(request: Any, tmp_path_factory: Any) -> Any:
    """Launch DBOS on a system database for the whole test session.

    The current process acts as the DBOS worker (queue workers, scheduler and
    recovery run in-process). Choose the system database with
    ``--dbos-system-db=auto|sqlite|postgres`` (default: auto — PostgreSQL via
    testcontainers when Docker is available, SQLite file otherwise).
    """
    import os

    mode = request.config.getoption('--dbos-system-db')
    container = None
    previous_url = os.environ.get('DBOS_SYSTEM_DATABASE_URL')

    if mode in ('postgres', 'auto'):
        try:
            from testcontainers.community.postgres import PostgresContainer

            container = PostgresContainer('postgres:17')
            container.start()
            system_database_url = container.get_connection_url().replace(
                'postgresql+psycopg2://', 'postgresql+psycopg://'
            )
        except Exception as exc:  # noqa: BLE001 - Docker/testcontainers errors are not typed
            if mode == 'postgres':
                pytest.skip(f'Docker/Postgres unavailable: {exc}')
            container = None
            system_database_url = f'sqlite:///{tmp_path_factory.mktemp("dbos") / "systemdb.sqlite"}'
            print(f'\nPostgres unavailable ({exc}); using SQLite system database.')
    else:
        system_database_url = f'sqlite:///{tmp_path_factory.mktemp("dbos") / "systemdb.sqlite"}'

    os.environ['DBOS_SYSTEM_DATABASE_URL'] = system_database_url

    from django_dbos import client, dbos_setup

    # The DBOSClient (enqueue/get_result) must target this session's system
    # database: drop any client built before the URL was set.
    client.close_client()
    dbos_setup.ensure_launched()
    yield system_database_url
    client.close_client()
    dbos_setup.shutdown()
    if previous_url is None:
        del os.environ['DBOS_SYSTEM_DATABASE_URL']
    else:
        os.environ['DBOS_SYSTEM_DATABASE_URL'] = previous_url
    if container is not None:
        container.stop()
