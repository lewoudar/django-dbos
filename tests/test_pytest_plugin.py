"""Tests for the pytest plugin, run through pytester (isolated pytest sessions)."""

from __future__ import annotations

import pytest

pytest_plugins = ['pytester']

SETTINGS = """
SECRET_KEY = "test"  # noqa: S105
DEBUG = True
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django_dbos",
    "tasks_app",
]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
TASKS = {
    "default": {"BACKEND": "django_dbos.backends.DBOSBackend"},
}
USE_TZ = True
"""

TASKS_MODULE = """
from django.tasks import task

from django_dbos import dbos_params


@task
def add(a, b):
    return a + b


@task
async def async_add(a, b):
    return a + b


@task
def say_hello():
    return "hello"


@task
def explode():
    raise ValueError("boom")


@task(takes_context=True)
def with_context(context):
    return context.task_result.id


@task(takes_context=True)
async def async_with_context(context):
    return context.task_result.id



@task
async def async_explode():
    raise ValueError("async boom")


@task
def interrupt():
    raise KeyboardInterrupt


@task
async def async_interrupt():
    raise KeyboardInterrupt


@dbos_params(durable_operations=True)
@task
def durable():
    return "durable"
"""

INLINE_TEST_SYNC = """
from django.tasks import TaskResultStatus
from tasks_app.tasks import add


def test_roundtrip(dbos_inline):
    result = add.enqueue(2, 3)
    assert result.status == TaskResultStatus.SUCCESSFUL
    assert result.return_value == 5
"""

INLINE_TEST_ASYNC = """
from django.tasks import TaskResultStatus
from tasks_app.tasks import async_add


def test_roundtrip(dbos_inline):
    result = async_add.enqueue(2, 3)
    assert result.status == TaskResultStatus.SUCCESSFUL
    assert result.return_value == 5
"""

INLINE_TEST_AENQUEUE = """
import asyncio

from django.tasks import TaskResultStatus
from tasks_app.tasks import add, async_add


async def _roundtrip():
    sync_result = await add.aenqueue(2, 3)
    async_result = await async_add.aenqueue(4, 5)
    fetched = await add.aget_result(sync_result.id)
    return sync_result, async_result, fetched


def test_async_roundtrip(dbos_inline):
    sync_result, async_result, fetched = asyncio.run(_roundtrip())
    assert (sync_result.return_value, async_result.return_value) == (5, 9)
    assert fetched.status == TaskResultStatus.SUCCESSFUL
"""

INLINE_TEST_FAILURE = """
from django.tasks import TaskResultStatus
from tasks_app.tasks import explode


def test_failure_is_a_failed_result(dbos_inline):
    result = explode.enqueue()
    assert result.status == TaskResultStatus.FAILED
    assert result.errors[0].exception_class is ValueError
    assert 'boom' in result.errors[0].traceback
"""

INLINE_TEST_CONTEXT = """
from tasks_app.tasks import with_context


def test_context_is_provided(dbos_inline):
    result = with_context.enqueue()
    # The context wraps the inline result itself: no system database is read.
    assert result.return_value == result.id
"""

INLINE_TEST_ASYNC_CONTEXT = """
import asyncio

from tasks_app.tasks import async_with_context


def test_async_context_is_provided(dbos_inline):
    result = asyncio.run(async_with_context.aenqueue())
    assert result.return_value == result.id
"""

INLINE_TEST_MISCONFIGURED_QUEUES = """
import pytest

from django_dbos.exceptions import ImproperlyConfiguredDbos
from tasks_app.tasks import add


def test_misconfigured_queues_are_rejected_inline_too(dbos_inline, settings):
    settings.TASKS = {
        "default": {
            "BACKEND": "django_dbos.backends.DBOSBackend",
            "QUEUES": ["default"],
            "OPTIONS": {"QUEUES": {"default": None}},
        },
    }
    with pytest.raises(ImproperlyConfiguredDbos, match="not both"):
        add.enqueue(1, 2)
"""

INLINE_TEST_GET_RESULT = """
from django.tasks import TaskResultStatus
from tasks_app.tasks import add


def test_get_result(dbos_inline):
    result = add.enqueue(1, 1)
    fetched = add.get_result(result.id)
    assert fetched.status == TaskResultStatus.SUCCESSFUL
    assert fetched.return_value == 2
"""

INLINE_TEST_EDGE_CASES = """
import asyncio

import pytest
from django.tasks import TaskResultStatus
from django.tasks.exceptions import TaskResultDoesNotExist
from tasks_app.tasks import add, async_explode, async_interrupt, interrupt


def test_async_failure_is_a_failed_result(dbos_inline):
    result = asyncio.run(async_explode.aenqueue())
    assert result.status == TaskResultStatus.FAILED
    assert 'async boom' in result.errors[0].traceback


def test_unknown_result_id_raises(dbos_inline):
    with pytest.raises(TaskResultDoesNotExist):
        add.get_result('never-enqueued')


def test_keyboard_interrupt_is_not_swallowed(dbos_inline):
    with pytest.raises(KeyboardInterrupt):
        interrupt.enqueue()


def test_async_keyboard_interrupt_is_not_swallowed(dbos_inline):
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(async_interrupt.aenqueue())
"""

# Makes `PostgresContainer.start()` fail as it does when Docker is not running.
UNAVAILABLE_POSTGRES_CONFTEST = """
import os
import sys
import types

module = types.ModuleType('testcontainers.community.postgres')


class PostgresContainer:
    def __init__(self, image):
        pass

    def start(self):
        raise RuntimeError('Docker is not running')


module.PostgresContainer = PostgresContainer
sys.modules['testcontainers.community.postgres'] = module
os.environ['DBOS_SYSTEM_DATABASE_URL'] = 'sqlite:///previous.sqlite'


def pytest_unconfigure(config):
    # Runs after the session fixtures are torn down.
    print('DBOS_SYSTEM_DATABASE_URL after the session:', os.environ.get('DBOS_SYSTEM_DATABASE_URL'))
"""

MISSING_TESTCONTAINERS_CONFTEST = """
import sys
from importlib.abc import MetaPathFinder


class BlockTestcontainers(MetaPathFinder):
    # Simulate an environment without the [test] extra's testcontainers,
    # raising the same error as a missing package. Not through
    # sys.modules[...] = None: on Python 3.15, formatting that error
    # re-imports the blocked parent package and crashes pytest.
    def find_spec(self, name, path=None, target=None):
        if name.partition('.')[0] == 'testcontainers':
            raise ModuleNotFoundError(f'No module named {name!r}', name=name)
        return None


sys.meta_path.insert(0, BlockTestcontainers())
"""

SYSTEM_DB_TEST = """
import os


def test_uses_sqlite(dbos_system):
    assert dbos_system.startswith('sqlite:///')
    assert os.environ['DBOS_SYSTEM_DATABASE_URL'] == dbos_system
"""


def _make_project(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(settings=SETTINGS)
    pytester.mkpydir('tasks_app')
    (pytester.path / 'tasks_app' / 'tasks.py').write_text(TASKS_MODULE, encoding='utf-8')
    pytester.makepyfile(conftest='')  # keep the rootdir clean


class TestDbosInline:
    @pytest.mark.parametrize(
        'inline_test_source',
        [
            pytest.param(INLINE_TEST_SYNC, id='sync-roundtrip'),
            pytest.param(INLINE_TEST_ASYNC, id='async-roundtrip'),
            pytest.param(INLINE_TEST_GET_RESULT, id='get-result'),
            pytest.param(INLINE_TEST_AENQUEUE, id='async-api'),
            pytest.param(INLINE_TEST_FAILURE, id='failure-is-a-failed-result'),
            pytest.param(INLINE_TEST_CONTEXT, id='takes-context'),
            pytest.param(INLINE_TEST_ASYNC_CONTEXT, id='async-takes-context'),
            pytest.param(INLINE_TEST_MISCONFIGURED_QUEUES, id='misconfigured-queues'),
        ],
    )
    def test_inline_roundtrip(self, pytester: pytest.Pytester, inline_test_source: str):
        _make_project(pytester)
        pytester.makepyfile(inline_test_source)
        result = pytester.runpytest_subprocess('--ds=settings', '-p', 'no:cacheprovider')
        result.assert_outcomes(passed=1)

    def test_inline_edge_cases(self, pytester: pytest.Pytester):
        _make_project(pytester)
        pytester.makepyfile(INLINE_TEST_EDGE_CASES)
        result = pytester.runpytest_subprocess('--ds=settings', '-p', 'no:cacheprovider')
        result.assert_outcomes(passed=4)

    @pytest.mark.parametrize('strict', [False, True], ids=['default', 'strict'])
    def test_durable_operations_are_rejected(self, pytester: pytest.Pytester, strict: bool):
        # Outside a workflow get_dbos_context() raises: a durable task could only fail.
        _make_project(pytester)
        pytester.makepyfile(
            """
            from tasks_app.tasks import add, durable


            def test_durable(dbos_inline):
                durable.enqueue()


            def test_regular_task_still_runs(dbos_inline):
                assert add.enqueue(1, 2).return_value == 3
            """
        )
        options = ['--dbos-inline-strict'] if strict else []
        result = pytester.runpytest_subprocess('--ds=settings', *options, '-p', 'no:cacheprovider')
        result.assert_outcomes(failed=1, passed=1)
        result.stdout.fnmatch_lines(['*uses durable operations*'])

    def test_deferred_task_runs_at_once_by_default(self, pytester: pytest.Pytester):
        _make_project(pytester)
        pytester.makepyfile(
            """
            from datetime import timedelta

            from django.tasks import TaskResultStatus
            from django.utils import timezone
            from tasks_app.tasks import add


            def test_deferred(dbos_inline):
                result = add.using(run_after=timezone.now() + timedelta(hours=1)).enqueue(1, 2)
                assert result.status == TaskResultStatus.SUCCESSFUL
                assert result.return_value == 3
            """
        )
        result = pytester.runpytest_subprocess('--ds=settings', '-p', 'no:cacheprovider')
        result.assert_outcomes(passed=1)

    def test_strict_mode_rejects_deferred_tasks(self, pytester: pytest.Pytester):
        _make_project(pytester)
        pytester.makepyfile(
            """
            from datetime import timedelta

            from django.utils import timezone
            from tasks_app.tasks import say_hello


            def test_deferred(dbos_inline):
                deferred = say_hello.using(run_after=timezone.now() + timedelta(seconds=10))
                deferred.enqueue()
            """
        )
        result = pytester.runpytest_subprocess('--ds=settings', '--dbos-inline-strict', '-p', 'no:cacheprovider')
        result.assert_outcomes(failed=1)
        result.stdout.fnmatch_lines(['*is deferred (run_after)*'])

    def test_dbos_system_without_testcontainers_explains_the_extra(self, pytester: pytest.Pytester):
        _make_project(pytester)
        pytester.makepyfile(conftest=MISSING_TESTCONTAINERS_CONFTEST)
        pytester.makepyfile(
            """
            def test_needs_postgres(dbos_system):
                pass
            """
        )
        result = pytester.runpytest_subprocess('--ds=settings', '--dbos-system-db=postgres', '-p', 'no:cacheprovider')
        result.assert_outcomes(errors=1)
        result.stdout.fnmatch_lines(['*install the django-dbos[[]test[]] extra*'])

    def test_plugin_does_not_break_non_django_sessions(self, pytester: pytest.Pytester):
        # The plugin module must load cleanly without Django configured.
        pytester.makepyfile('def test_ok(): assert True')
        result = pytester.runpytest_subprocess('-p', 'no:cacheprovider')
        result.assert_outcomes(passed=1)


class TestDbosSystem:
    def test_sqlite_mode_never_starts_a_container(self, pytester: pytest.Pytester):
        _make_project(pytester)
        pytester.makepyfile(conftest=UNAVAILABLE_POSTGRES_CONFTEST)
        pytester.makepyfile(SYSTEM_DB_TEST)
        result = pytester.runpytest_subprocess(
            '--ds=settings', '--dbos-system-db=sqlite', '-s', '-p', 'no:cacheprovider'
        )
        result.assert_outcomes(passed=1)
        result.stdout.no_fnmatch_line('*Postgres unavailable*')

    def test_auto_mode_falls_back_to_sqlite_and_restores_the_url(self, pytester: pytest.Pytester):
        _make_project(pytester)
        pytester.makepyfile(conftest=UNAVAILABLE_POSTGRES_CONFTEST)
        pytester.makepyfile(SYSTEM_DB_TEST)
        result = pytester.runpytest_subprocess('--ds=settings', '-s', '-p', 'no:cacheprovider')
        result.assert_outcomes(passed=1)
        result.stdout.fnmatch_lines(
            [
                '*Postgres unavailable (Docker is not running); using SQLite system database.*',
                'DBOS_SYSTEM_DATABASE_URL after the session: sqlite:///previous.sqlite',
            ]
        )

    def test_auto_mode_without_testcontainers_falls_back_to_sqlite(self, pytester: pytest.Pytester):
        _make_project(pytester)
        pytester.makepyfile(conftest=MISSING_TESTCONTAINERS_CONFTEST)
        pytester.makepyfile(SYSTEM_DB_TEST)
        result = pytester.runpytest_subprocess('--ds=settings', '-s', '-p', 'no:cacheprovider')
        result.assert_outcomes(passed=1)
        result.stdout.fnmatch_lines(["*Postgres unavailable (No module named 'testcontainers*'); using SQLite*"])

    def test_fixture_url_wins_over_the_configured_one(self, pytester: pytest.Pytester):
        # OPTIONS["SYSTEM_DATABASE_URL"] wins over the environment variable:
        # the fixture must override it, or the session runs on this database.
        _make_project(pytester)
        pytester.makepyfile(
            settings=SETTINGS.replace(
                '{"BACKEND": "django_dbos.backends.DBOSBackend"}',
                '{"BACKEND": "django_dbos.backends.DBOSBackend",'
                ' "OPTIONS": {"SYSTEM_DATABASE_URL": "sqlite:///configured.sqlite"}}',
            )
        )
        pytester.makepyfile(
            conftest="""
            from django.conf import settings


            def pytest_unconfigure(config):
                print('URL after the session:', settings.TASKS['default']['OPTIONS']['SYSTEM_DATABASE_URL'])
            """
        )
        pytester.makepyfile(
            """
            import time

            from django.tasks import TaskResultStatus

            from django_dbos import settings as dbos_settings
            from tasks_app.tasks import add


            def test_runs_on_the_fixture_database(dbos_system):
                assert dbos_settings.resolve_system_database_url() == dbos_system
                result = add.enqueue(1, 2)
                deadline = time.monotonic() + 30
                while result.status != TaskResultStatus.SUCCESSFUL and time.monotonic() < deadline:
                    time.sleep(0.1)
                    result.refresh()
                assert result.return_value == 3
            """
        )
        result = pytester.runpytest_subprocess(
            '--ds=settings', '--dbos-system-db=sqlite', '-s', '-p', 'no:cacheprovider'
        )
        result.assert_outcomes(passed=1)
        result.stdout.fnmatch_lines(['URL after the session: sqlite:///configured.sqlite'])
        assert not (pytester.path / 'configured.sqlite').exists()

    def test_postgres_mode_skips_when_postgres_is_unavailable(self, pytester: pytest.Pytester):
        _make_project(pytester)
        pytester.makepyfile(conftest=UNAVAILABLE_POSTGRES_CONFTEST)
        pytester.makepyfile(SYSTEM_DB_TEST)
        result = pytester.runpytest_subprocess(
            '--ds=settings', '--dbos-system-db=postgres', '-rs', '-p', 'no:cacheprovider'
        )
        result.assert_outcomes(skipped=1)
        result.stdout.fnmatch_lines(['*Docker/Postgres unavailable: Docker is not running*'])
