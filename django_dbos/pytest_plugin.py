"""Pytest plugin for django-dbos.

Distributed through the ``pytest11`` entry point, so it loads automatically
whenever django-dbos and pytest are installed — no configuration needed.
Its dependencies come with the ``django-dbos[test]`` extra (pytest and
testcontainers; pytest-django is the natural companion). Everything here imports Django/DBOS lazily
(inside fixtures), because pytest loads plugins before ``django.setup()``
and even for non-Django test suites.

Fixtures:

- ``dbos_inline``: patch the DBOS backend so enqueued tasks execute
  synchronously in-process, without a DBOS system database — sync and async
  enqueues alike, a failing task yielding a FAILED result like the real
  backend. Serves the majority of test needs.
- ``dbos_system``: run a real (in-process) DBOS worker on a system database —
  SQLite file by default, a PostgreSQL container when
  ``--dbos-system-db=postgres`` is passed (requires Docker and
  ``testcontainers[postgres]``). Use it when you need real semantics:
  step retries, durable sleeps, schedules. Enqueues go through the same
  ``DBOSClient`` path as production; the launched DBOS executes them.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from traceback import format_exception
from typing import Any

import pytest
from django.tasks import TaskResult, TaskResultStatus
from django.tasks.base import TaskError
from django.tasks.exceptions import TaskResultDoesNotExist

from django_dbos import client, dbos_setup
from django_dbos.backends import DBOSBackend
from django_dbos.exceptions import DjangoDbosError
from django_dbos.tasks_registry import _build_task_context


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


def _inline_result(task: Any, args: tuple[Any, ...], kwargs: dict[str, Any], outcome: Any) -> Any:
    """A finished TaskResult: SUCCESSFUL with the return value, or FAILED with the exception."""
    now = _now()
    failed = isinstance(outcome, BaseException)
    errors = []
    if failed:
        exception_type = type(outcome)
        errors = [
            TaskError(
                exception_class_path=f'{exception_type.__module__}.{exception_type.__qualname__}',
                traceback=''.join(format_exception(outcome)),
            )
        ]
    result = TaskResult(
        task=task,
        id=str(uuid.uuid4()),
        status=TaskResultStatus.FAILED if failed else TaskResultStatus.SUCCESSFUL,
        enqueued_at=now,
        started_at=now,
        finished_at=now,
        last_attempted_at=now,
        args=list(args),
        kwargs=dict(kwargs),
        backend=task.backend,
        errors=errors,
        worker_ids=['inline'],
    )
    if not failed:
        object.__setattr__(result, '_return_value', outcome)
    _results[result.id] = result
    return result


def _check_inline_support(backend: Any, task: Any) -> None:
    """Mirror the real backend's validation, and refuse what inline cannot reproduce in strict mode."""
    backend.validate_task(task)
    backend._validate_queue(task)
    if not backend._inline_strict:
        return
    if task.run_after is not None:
        raise DjangoDbosError(
            f"Task '{task.name}' is deferred (run_after) and cannot be "
            'executed inline. Test the task body with run_task() or use '
            'the dbos_system fixture.'
        )
    if task.dbos_params.durable_operations:
        raise DjangoDbosError(
            f"Task '{task.name}' uses durable operations, which the inline "
            'mode cannot reproduce. Use the dbos_system fixture.'
        )


def _inline_args(task: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[Any, ...]:
    if not task.takes_context:
        return tuple(args)
    return (_build_task_context(task, args, kwargs), *args)


def _inline_enqueue(self: Any, task: Any, args: Any, kwargs: Any) -> Any:
    """Run the task now, like Django's immediate backend: a failure is a FAILED result, not an exception."""
    _check_inline_support(self, task)
    call_args = _inline_args(task, args, kwargs)
    try:
        outcome = task.call(*call_args, **kwargs)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # noqa: BLE001 - reported as a FAILED result, like the real backend
        outcome = exc
    return _inline_result(task, tuple(args), kwargs, outcome)


async def _inline_aenqueue(self: Any, task: Any, args: Any, kwargs: Any) -> Any:
    """Async variant: awaits coroutine tasks directly (no async_to_sync inside the running loop)."""
    _check_inline_support(self, task)
    call_args = _inline_args(task, args, kwargs)
    try:
        outcome = await task.acall(*call_args, **kwargs)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # noqa: BLE001 - reported as a FAILED result, like the real backend
        outcome = exc
    return _inline_result(task, tuple(args), kwargs, outcome)


_results: dict[str, Any] = {}


def _lookup_result(self: Any, result_id: str) -> Any:
    try:
        return _results[result_id]
    except KeyError:
        raise TaskResultDoesNotExist(result_id) from None


async def _alookup_result(self: Any, result_id: str) -> Any:
    return _lookup_result(self, result_id)


@pytest.fixture
def dbos_inline(monkeypatch: Any, request: Any) -> Any:
    """Make DBOS-backed tasks execute synchronously, without a system database."""
    strict = bool(request.config.getoption('--dbos-inline-strict'))
    monkeypatch.setattr(DBOSBackend, '_inline_strict', strict, raising=False)
    monkeypatch.setattr(DBOSBackend, 'enqueue', _inline_enqueue)
    monkeypatch.setattr(DBOSBackend, 'aenqueue', _inline_aenqueue)
    monkeypatch.setattr(DBOSBackend, 'get_result', _lookup_result)
    monkeypatch.setattr(DBOSBackend, 'aget_result', _alookup_result)
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
    mode = request.config.getoption('--dbos-system-db')
    container = None
    previous_url = os.environ.get('DBOS_SYSTEM_DATABASE_URL')

    if mode in ('postgres', 'auto'):
        try:
            try:
                from testcontainers.community.postgres import PostgresContainer
            except ImportError:
                pytest.fail(
                    'The dbos_system fixture needs testcontainers for PostgreSQL: '
                    'install the django-dbos[test] extra (or pass --dbos-system-db=sqlite).',
                    pytrace=False,
                )

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
