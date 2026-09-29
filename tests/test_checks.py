"""Tests for the django-dbos system checks."""

import dataclasses
import sys
import types

import pytest

from django_dbos import checks, discovery
from django_dbos import tasks as tasks_module
from django_dbos.checks import check_dbos_settings, check_system_database_engine, check_task_modules
from tests import tasks as test_tasks


class TestCheckDbosSettings:
    def test_valid_settings_report_no_error(self, settings, monkeypatch):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        settings.DJANGO_DBOS = {}
        assert check_dbos_settings() == []

    def test_invalid_settings_report_an_error(self, settings, monkeypatch):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        settings.DJANGO_DBOS = {'QUEUES': ['emails']}  # must be a mapping
        errors = check_dbos_settings()
        assert len(errors) == 1
        assert errors[0].id == 'django_dbos.E001'
        assert 'QUEUES' in errors[0].msg


class TestCheckSystemDatabaseEngine:
    @pytest.fixture()
    def engine(self, mocker):
        def _set(engine: str) -> None:
            mocker.patch.object(checks, '_default_database_engine', return_value=engine)

        return _set

    def test_explicit_url_reports_nothing(self, settings, engine):
        engine('django.db.backends.oracle')
        settings.DJANGO_DBOS = {'SYSTEM_DATABASE_URL': 'postgresql://explicit'}
        assert check_system_database_engine() == []

    @pytest.mark.parametrize(
        'engine_name',
        ['django.db.backends.postgresql', 'django.db.backends.sqlite3'],
    )
    def test_supported_engine_reports_nothing(self, settings, engine, engine_name):
        engine(engine_name)
        settings.DJANGO_DBOS = {}
        assert check_system_database_engine() == []

    def test_unsupported_engine_reports_an_info(self, settings, engine):
        engine('django.db.backends.oracle')
        settings.DJANGO_DBOS = {}
        infos = check_system_database_engine()
        assert len(infos) == 1
        assert infos[0].id == 'django_dbos.I001'
        assert 'SYSTEM_DATABASE_URL' in infos[0].hint


def test_default_database_engine_reads_the_default_connection():
    assert checks._default_database_engine() == 'django.db.backends.sqlite3'


def placeholder() -> None:
    """Body of the tasks built by ``task_in``."""


@pytest.fixture()
def task_in(monkeypatch):
    """Build a DBOS task whose function lives in the given module."""
    # Building a task records it among the known tasks: keep the registry intact.
    monkeypatch.setattr(tasks_module, '_known_tasks', dict(tasks_module._known_tasks))

    def factory(module: str, **changes):
        func = types.FunctionType(placeholder.__code__, placeholder.__globals__, 'placeholder')
        func.__module__ = module
        return dataclasses.replace(test_tasks.add, func=func, **changes)

    return factory


@pytest.fixture()
def discovered(mocker, monkeypatch):
    """Make discovery report the given modules, registered in ``sys.modules`` with the given attributes."""

    def _set(**modules: dict) -> None:
        for name, attributes in modules.items():
            module = types.ModuleType(name)
            vars(module).update(attributes)
            monkeypatch.setitem(sys.modules, name, module)
        mocker.patch.object(discovery, 'autodiscover_tasks', return_value=list(modules))

    return _set


class TestCheckTaskModules:
    def test_tasks_of_discovered_modules_report_nothing(self, mocker):
        mocker.patch.object(checks, '_known_tasks', return_value=(test_tasks.add, test_tasks.send_email))
        assert check_task_modules() == []

    def test_task_in_a_submodule_of_a_discovered_package_reports_nothing(self, mocker, task_in, discovered):
        discovered(**{'shop.tasks': {}})
        mocker.patch.object(checks, '_known_tasks', return_value=(task_in('shop.tasks.emails'),))
        assert check_task_modules() == []

    def test_task_exposed_by_a_discovered_module_reports_nothing(self, mocker, task_in, discovered):
        task = task_in('shop.utils')
        discovered(**{'shop.tasks': {'placeholder': task}})
        mocker.patch.object(checks, '_known_tasks', return_value=(task,))
        assert check_task_modules() == []

    @pytest.mark.parametrize('module', ['shop.utils', 'shop.tasks_extra'])
    def test_task_the_worker_does_not_import_reports_a_warning(self, mocker, task_in, discovered, module):
        discovered(**{'shop.tasks': {}})
        mocker.patch.object(checks, '_known_tasks', return_value=(task_in(module),))

        warnings = check_task_modules()

        assert len(warnings) == 1
        assert warnings[0].id == 'django_dbos.W001'
        assert f"'{module}.placeholder'" in warnings[0].msg

    def test_import_failure_reports_an_error(self, mocker):
        error = ImportError('cannot connect to postgresql://user:hunter2@db/app')
        mocker.patch.object(discovery, 'autodiscover_tasks', side_effect=error)

        errors = check_task_modules()

        assert len(errors) == 1
        assert errors[0].id == 'django_dbos.E004'
        assert 'ImportError' in errors[0].msg
        assert 'hunter2' not in errors[0].msg


def test_known_tasks_are_the_tasks_declared_in_this_process():
    assert test_tasks.add in checks._known_tasks()
