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
    "default": {"BACKEND": "django_dbos.backends.DBOSBackend", "QUEUES": ["default"]},
}
USE_TZ = True
DJANGO_DBOS = {"AUTO_INIT": False}
"""

TASKS_MODULE = """
from django.tasks import task


@task
def add(a, b):
    return a + b


@task
async def async_add(a, b):
    return a + b


@task
def say_hello():
    return "hello"
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

INLINE_TEST_GET_RESULT = """
from django.tasks import TaskResultStatus
from tasks_app.tasks import add


def test_get_result(dbos_inline):
    result = add.enqueue(1, 1)
    fetched = add.get_result(result.id)
    assert fetched.status == TaskResultStatus.SUCCESSFUL
    assert fetched.return_value == 2
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
        ],
    )
    def test_inline_roundtrip(self, pytester: pytest.Pytester, inline_test_source: str):
        _make_project(pytester)
        pytester.makepyfile(inline_test_source)
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

    def test_plugin_does_not_break_non_django_sessions(self, pytester: pytest.Pytester):
        # The plugin module must load cleanly without Django configured.
        pytester.makepyfile('def test_ok(): assert True')
        result = pytester.runpytest_subprocess('-p', 'no:cacheprovider')
        result.assert_outcomes(passed=1)
