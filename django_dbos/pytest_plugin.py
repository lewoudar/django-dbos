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
from django.tasks import TaskContext, TaskResult, TaskResultStatus
from django.tasks.base import TaskError
from django.tasks.exceptions import TaskResultDoesNotExist

from django_dbos import client, dbos_setup
from django_dbos.backends import DBOSBackend
from django_dbos.exceptions import DjangoDbosError
from django_dbos.tasks import DBOSTask


def pytest_addoption(parser: Any) -> None:
    group = parser.getgroup('django-dbos')
    group.addoption(
        '--dbos-inline-strict',
        action='store_true',
        default=False,
        help="Make django-dbos' dbos_inline fixture refuse deferred tasks (run_after) instead of running them at once.",
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


def _running_result(task: DBOSTask, args: tuple[Any, ...], kwargs: dict[str, Any]) -> TaskResult:
    """The RUNNING TaskResult of an inline execution, also handed to the task through its TaskContext."""
    now = _now()
    return TaskResult(
        task=task,
        id=str(uuid.uuid4()),
        status=TaskResultStatus.RUNNING,
        enqueued_at=now,
        started_at=now,
        finished_at=None,
        last_attempted_at=now,
        args=list(args),
        kwargs=dict(kwargs),
        backend=task.backend,
        errors=[],
        worker_ids=['inline'],
    )


def _finish_result(result: TaskResult, outcome: Any) -> TaskResult:
    """Complete ``result`` in place: SUCCESSFUL with the return value, or FAILED with the exception."""
    object.__setattr__(result, 'finished_at', _now())
    if isinstance(outcome, BaseException):
        exception_type = type(outcome)
        result.errors.append(
            TaskError(
                exception_class_path=f'{exception_type.__module__}.{exception_type.__qualname__}',
                traceback=''.join(format_exception(outcome)),
            )
        )
        object.__setattr__(result, 'status', TaskResultStatus.FAILED)
    else:
        object.__setattr__(result, 'status', TaskResultStatus.SUCCESSFUL)
        object.__setattr__(result, '_return_value', outcome)
    _results[result.id] = result
    return result


def _check_inline_support(backend: Any, task: DBOSTask) -> None:
    """Mirror the real backend's validation, and refuse what inline cannot reproduce.

    Durable operations are always refused: outside a DBOS workflow,
    ``get_dbos_context()`` raises, so the task could only yield a FAILED
    result. A deferred task can run at once, unless in strict mode.
    """
    backend._validate_queues_config()
    backend.validate_task(task)
    if task.dbos_params.durable_operations:
        raise DjangoDbosError(
            f"Task '{task.name}' uses durable operations, which the inline "
            'mode cannot reproduce. Use the dbos_system fixture.'
        )
    if backend._inline_strict and task.run_after is not None:
        raise DjangoDbosError(
            f"Task '{task.name}' is deferred (run_after) and cannot be "
            'executed inline. Test the task body with run_task() or use '
            'the dbos_system fixture.'
        )


def _inline_args(result: TaskResult, args: tuple[Any, ...]) -> tuple[Any, ...]:
    """The positional arguments of the call, preceded by a TaskContext when the task takes one.

    The context wraps the inline result itself, as Django's immediate backend
    does: no system database is read, and ``context.task_result.id`` is the id
    ``enqueue()`` returns.
    """
    if not result.task.takes_context:
        return tuple(args)
    return (TaskContext(task_result=result), *args)


def _inline_enqueue(self: Any, task: DBOSTask, args: Any, kwargs: Any) -> TaskResult:
    """Run the task now, like Django's immediate backend: a failure is a FAILED result, not an exception."""
    _check_inline_support(self, task)
    result = _running_result(task, args, kwargs)
    try:
        outcome = task.call(*_inline_args(result, args), **kwargs)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # noqa: BLE001 - reported as a FAILED result, like the real backend
        outcome = exc
    return _finish_result(result, outcome)


async def _inline_aenqueue(self: Any, task: DBOSTask, args: Any, kwargs: Any) -> TaskResult:
    """Async variant: awaits coroutine tasks directly (no async_to_sync inside the running loop)."""
    _check_inline_support(self, task)
    result = _running_result(task, args, kwargs)
    try:
        outcome = await task.acall(*_inline_args(result, args), **kwargs)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # noqa: BLE001 - reported as a FAILED result, like the real backend
        outcome = exc
    return _finish_result(result, outcome)


_results: dict[str, TaskResult] = {}


def _lookup_result(self: Any, result_id: str) -> TaskResult:
    try:
        return _results[result_id]
    except KeyError:
        raise TaskResultDoesNotExist(result_id) from None


async def _alookup_result(self: Any, result_id: str) -> TaskResult:
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


def _override_system_database_url(url: str) -> Any:
    """Point the DBOS backend's ``OPTIONS["SYSTEM_DATABASE_URL"]`` at ``url`` when the settings set it.

    The option wins over the ``DBOS_SYSTEM_DATABASE_URL`` environment
    variable: without this override, a project setting it (from
    ``os.environ`` at settings import, typically) would run the session on
    its configured system database instead of the fixture's. Without the
    option, the environment variable is enough and stays the one tests may
    change (``monkeypatch.setenv``).

    Args:
        url: The system database URL of the session.

    Returns:
        The enabled ``override_settings``, to disable at teardown; ``None``
        when the settings leave the option unset.
    """
    from django.conf import settings
    from django.test.utils import override_settings

    from django_dbos.settings import dbos_backend_aliases, get_dbos_setting

    if not get_dbos_setting('SYSTEM_DATABASE_URL'):
        return None
    alias = dbos_backend_aliases()[0]
    params = settings.TASKS[alias]
    override = override_settings(
        TASKS={**settings.TASKS, alias: {**params, 'OPTIONS': {**params['OPTIONS'], 'SYSTEM_DATABASE_URL': url}}}
    )
    override.enable()
    return override


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
                if mode == 'postgres':
                    pytest.fail(
                        'The dbos_system fixture needs testcontainers for PostgreSQL: '
                        'install the django-dbos[test] extra (or pass --dbos-system-db=sqlite).',
                        pytrace=False,
                    )
                raise  # auto: falls back to SQLite below

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
    override = _override_system_database_url(system_database_url)

    # The DBOSClient (enqueue/get_result) must target this session's system
    # database: drop any client built before the URL was set.
    client.close_client()
    dbos_setup.ensure_launched()
    yield system_database_url
    client.close_client()
    dbos_setup.shutdown()
    if override is not None:
        override.disable()
    if previous_url is None:
        del os.environ['DBOS_SYSTEM_DATABASE_URL']
    else:
        os.environ['DBOS_SYSTEM_DATABASE_URL'] = previous_url
    if container is not None:
        container.stop()
