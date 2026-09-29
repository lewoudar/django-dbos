"""Tests for the django-dbos system checks."""

import dataclasses
import sys
import types

import pytest
from django.core.checks import Tags
from django.core.checks.registry import registry

from django_dbos import checks, discovery
from django_dbos import settings as settings_module
from django_dbos import tasks as tasks_module
from django_dbos.checks import (
    check_dbos_settings,
    check_declared_queues,
    check_executor_id,
    check_in_memory_system_database,
    check_serializer,
    check_system_database_engine,
    check_task_modules,
    check_unknown_settings,
)
from django_dbos.exceptions import ImproperlyConfiguredDbos
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


class TestCheckUnknownSettings:
    def test_known_keys_report_nothing(self, settings):
        settings.DJANGO_DBOS = {'QUEUES': {'default': None}, 'EXECUTOR_ID': 'worker-1'}
        assert check_unknown_settings() == []

    def test_a_setting_that_is_not_a_mapping_is_left_to_e001(self, settings):
        settings.DJANGO_DBOS = ['QUEUES']
        assert check_unknown_settings() == []

    def test_a_typo_suggests_the_closest_key(self, settings):
        settings.DJANGO_DBOS = {'QUEUE': {'default': None}}

        errors = check_unknown_settings()

        assert len(errors) == 1
        assert errors[0].id == 'django_dbos.E003'
        assert "'QUEUE'" in errors[0].msg
        assert errors[0].hint == "Did you mean 'QUEUES'?"

    def test_an_unrelated_key_lists_the_valid_ones(self, settings):
        settings.DJANGO_DBOS = {'COLOUR': 'blue'}

        errors = check_unknown_settings()

        assert len(errors) == 1
        assert errors[0].hint.startswith('Valid keys: NAME, ')


DBOS_BACKEND = 'django_dbos.backends.DBOSBackend'


class TestCheckDeclaredQueues:
    @pytest.fixture(autouse=True)
    def tasks(self, mocker):
        mocker.patch.object(checks, '_known_tasks', return_value=(test_tasks.add, test_tasks.send_email))

    def test_declared_queues_report_nothing(self):
        assert check_declared_queues() == []

    def test_undeclared_queue_of_a_backend_reports_an_error(self, settings):
        settings.DJANGO_DBOS = {'QUEUES': {'default': None}}

        errors = check_declared_queues()

        assert len(errors) == 1
        assert errors[0].id == 'django_dbos.E002'
        assert errors[0].msg.startswith("Queue 'emails' of TASKS['dbos'] is not declared")
        assert errors[0].hint == "Add it to DJANGO_DBOS['QUEUES'], for example {'emails': None}."

    def test_implicit_default_queue_must_be_declared(self, settings):
        settings.TASKS = {'dbos': {'BACKEND': DBOS_BACKEND}}
        settings.DJANGO_DBOS = {'QUEUES': {'emails': None}}

        errors = check_declared_queues()

        assert [error.msg.split(' of ')[0] for error in errors] == ["Queue 'default'"]

    def test_backend_accepting_any_queue_checks_the_tasks(self, settings, mocker):
        mocker.patch.object(discovery, 'autodiscover_tasks', side_effect=ImportError('reported by E004'))
        settings.TASKS = {'dbos': {'BACKEND': DBOS_BACKEND, 'QUEUES': []}}
        settings.DJANGO_DBOS = {'QUEUES': {'default': None}}

        errors = check_declared_queues()

        assert len(errors) == 1
        assert errors[0].msg.startswith("Queue 'emails' of task 'tests.tasks.send_email' is not declared")

    def test_other_and_invalid_backends_are_ignored(self, settings):
        settings.TASKS = {
            'default': {'BACKEND': 'django.tasks.backends.immediate.ImmediateBackend', 'QUEUES': ['nowhere']},
            'missing': {'BACKEND': 'no.such.Backend'},
            'incomplete': {},
            'not_a_class': {'BACKEND': 'django_dbos.checks.check_declared_queues'},
        }
        settings.DJANGO_DBOS = {'QUEUES': {'default': None}}
        assert check_declared_queues() == []

    def test_invalid_queues_setting_is_left_to_e001(self, settings):
        settings.DJANGO_DBOS = {'QUEUES': ['emails']}
        assert check_declared_queues() == []


class TestCheckInMemorySystemDatabase:
    @pytest.fixture(autouse=True)
    def no_env_url(self, monkeypatch):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)

    @pytest.mark.parametrize(
        'dbos_settings',
        [{'SYSTEM_DATABASE_URL': 'sqlite:///:memory:'}, {}],  # explicit, derived from the in-memory test database
    )
    def test_in_memory_database_reports_a_warning(self, settings, dbos_settings):
        settings.DJANGO_DBOS = dbos_settings

        warnings = check_in_memory_system_database()

        assert len(warnings) == 1
        assert warnings[0].id == 'django_dbos.W002'

    @pytest.mark.parametrize('url', ['sqlite:///db.sqlite3-dbos.sqlite', 'postgresql://db/dbos_app'])
    def test_shared_database_reports_nothing(self, settings, url):
        settings.DJANGO_DBOS = {'SYSTEM_DATABASE_URL': url}
        assert check_in_memory_system_database() == []

    def test_unresolvable_url_is_left_to_e001(self, mocker):
        mocker.patch.object(
            settings_module, 'resolve_system_database_url', side_effect=ImproperlyConfiguredDbos('no database')
        )
        assert check_in_memory_system_database() == []


class TestDeploymentChecks:
    @pytest.fixture(autouse=True)
    def no_env(self, monkeypatch):
        monkeypatch.delenv('DBOS__VMID', raising=False)

    def test_they_only_run_with_deploy(self):
        deploy_checks = set(registry.get_checks(include_deployment_checks=True))
        default_checks = set(registry.get_checks())
        assert {check_serializer, check_executor_id} <= deploy_checks - default_checks

    def test_serializer_check_is_a_security_check(self):
        assert Tags.security in check_serializer.tags

    def test_defaults_report_pickle_and_the_executor_id(self, settings):
        settings.DJANGO_DBOS = {}
        assert [message.id for message in check_serializer()] == ['django_dbos.W003']
        assert [message.id for message in check_executor_id()] == ['django_dbos.W004']

    def test_configured_deployment_reports_nothing(self, settings):
        settings.DJANGO_DBOS = {'SERIALIZER': 'dbos.DBOSPortableJSONSerializer', 'EXECUTOR_ID': 'worker-1'}
        assert check_serializer() == []
        assert check_executor_id() == []

    def test_executor_id_from_the_environment(self, settings, monkeypatch):
        settings.DJANGO_DBOS = {}
        monkeypatch.setenv('DBOS__VMID', 'machine-1')
        assert check_executor_id() == []


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
