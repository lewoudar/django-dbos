"""Tests for the django-dbos system checks."""

import dataclasses
import sys
import types

import pytest
from django.core.checks import Tags
from django.core.checks.registry import registry

from django_dbos import checks, dbos_params, discovery
from django_dbos import settings as settings_module
from django_dbos.checks import (
    check_cockroachdb_migrations,
    check_dbos_backend_configured,
    check_dbos_settings,
    check_executor_id,
    check_in_memory_system_database,
    check_partitioned_queue_tasks,
    check_serializer,
    check_single_dbos_backend,
    check_step_options_with_durable_operations,
    check_task_modules,
    check_unknown_settings,
)
from django_dbos.exceptions import ImproperlyConfiguredDbos
from tests import tasks as test_tasks
from tests.helpers import set_dbos_options

DBOS_BACKEND = 'django_dbos.backends.DBOSBackend'


class TestCheckDbosSettings:
    def test_valid_settings_report_no_error(self, settings, monkeypatch):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        set_dbos_options(settings, {})
        assert check_dbos_settings() == []

    def test_invalid_settings_report_an_error(self, settings, monkeypatch):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        set_dbos_options(settings, {'QUEUES': ['emails']})  # must be a mapping
        errors = check_dbos_settings()
        assert len(errors) == 1
        assert errors[0].id == 'django_dbos.E001'
        assert 'QUEUES' in errors[0].msg

    def test_the_hint_points_at_the_backend_entry(self, settings, monkeypatch):
        # a top-level QUEUES error lives outside OPTIONS: the hint names the whole entry
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        settings.TASKS = {'dbos': {'BACKEND': DBOS_BACKEND, 'QUEUES': []}}
        errors = check_dbos_settings()
        assert [error.id for error in errors] == ['django_dbos.E001']
        assert errors[0].hint.startswith("Fix TASKS['dbos'] or ")


class TestCheckSingleDbosBackend:
    def test_one_backend_reports_nothing(self):
        assert check_single_dbos_backend() == []

    def test_several_backends_report_an_error(self, settings):
        settings.TASKS = {'dbos': {'BACKEND': DBOS_BACKEND}, 'other': {'BACKEND': DBOS_BACKEND}}

        errors = check_single_dbos_backend()

        assert len(errors) == 1
        assert errors[0].id == 'django_dbos.E002'
        assert "('dbos', 'other')" in errors[0].msg
        assert "only reads TASKS['dbos']" in errors[0].msg

    def test_other_and_invalid_backends_are_ignored(self, settings):
        settings.TASKS = {
            'default': {'BACKEND': 'django.tasks.backends.immediate.ImmediateBackend'},
            'missing': {'BACKEND': 'no.such.Backend'},
            'incomplete': {},
            'not_a_class': {'BACKEND': 'django_dbos.checks.check_single_dbos_backend'},
            'dbos': {'BACKEND': DBOS_BACKEND},
        }
        assert settings_module.dbos_backend_aliases() == ['dbos']
        assert check_single_dbos_backend() == []


class TestCheckUnknownSettings:
    def test_known_keys_report_nothing(self, settings):
        set_dbos_options(settings, {'QUEUES': {'default': None}, 'EXECUTOR_ID': 'worker-1'})
        assert check_unknown_settings() == []

    def test_a_setting_that_is_not_a_mapping_is_left_to_e001(self, settings):
        set_dbos_options(settings, ['QUEUES'])
        assert check_unknown_settings() == []

    def test_a_typo_suggests_the_closest_key(self, settings):
        set_dbos_options(settings, {'QUEUE': {'default': None}})

        errors = check_unknown_settings()

        assert len(errors) == 1
        assert errors[0].id == 'django_dbos.E003'
        assert "'QUEUE'" in errors[0].msg
        assert errors[0].hint == "Did you mean 'QUEUES'?"

    def test_an_unrelated_key_lists_the_valid_ones(self, settings):
        set_dbos_options(settings, {'COLOUR': 'blue'})

        errors = check_unknown_settings()

        assert len(errors) == 1
        assert errors[0].hint.startswith('Valid keys: NAME, ')


def placeholder() -> None:
    """Body of the tasks built by ``task_in``."""


@pytest.fixture()
def task_in(keep_known_tasks):
    """Build a DBOS task whose function lives in the given module."""

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
        mocker.patch.object(checks, 'known_tasks', return_value=(test_tasks.add, test_tasks.send_email))
        assert check_task_modules() == []

    def test_task_in_a_submodule_of_a_discovered_package_reports_nothing(self, mocker, task_in, discovered):
        discovered(**{'shop.tasks': {}})
        mocker.patch.object(checks, 'known_tasks', return_value=(task_in('shop.tasks.emails'),))
        assert check_task_modules() == []

    def test_task_exposed_by_a_discovered_module_reports_nothing(self, mocker, task_in, discovered):
        task = task_in('shop.utils')
        discovered(**{'shop.tasks': {'placeholder': task}})
        mocker.patch.object(checks, 'known_tasks', return_value=(task,))
        assert check_task_modules() == []

    @pytest.mark.parametrize('module', ['shop.utils', 'shop.tasks_extra'])
    def test_task_the_worker_does_not_import_reports_a_warning(self, mocker, task_in, discovered, module):
        discovered(**{'shop.tasks': {}})
        mocker.patch.object(checks, 'known_tasks', return_value=(task_in(module),))

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


class TestCheckCockroachDbMigrations:
    URL = 'cockroachdb+psycopg2://root@crdb:26257/dbos'

    @pytest.fixture(autouse=True)
    def no_env_url(self, monkeypatch):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)

    def test_cockroachdb_without_migrations_reports_an_error(self, settings):
        set_dbos_options(settings, {'SYSTEM_DATABASE_URL': self.URL, 'RUN_MIGRATIONS': False})

        errors = check_cockroachdb_migrations()

        assert [error.id for error in errors] == ['django_dbos.E005']
        assert "TASKS['dbos']['OPTIONS']['RUN_MIGRATIONS'] is False" in errors[0].msg

    @pytest.mark.parametrize(
        'options',
        [
            pytest.param({'SYSTEM_DATABASE_URL': URL}, id='cockroachdb-migrating'),
            pytest.param({'SYSTEM_DATABASE_URL': URL, 'RUN_MIGRATIONS': True}, id='cockroachdb-explicit'),
            pytest.param({'SYSTEM_DATABASE_URL': 'postgresql://db', 'RUN_MIGRATIONS': False}, id='postgres'),
            pytest.param(
                {'SYSTEM_DATABASE_URL': 'cockroachdb+psycopg://db', 'RUN_MIGRATIONS': False}, id='bad-url-left-to-e001'
            ),
            pytest.param({'SYSTEM_DATABASE_URL': URL, 'RUN_MIGRATIONS': 'false'}, id='bad-value-left-to-e001'),
        ],
    )
    def test_other_configurations_report_nothing(self, settings, options):
        set_dbos_options(settings, options)
        assert check_cockroachdb_migrations() == []


class TestCheckInMemorySystemDatabase:
    @pytest.fixture(autouse=True)
    def no_env_url(self, monkeypatch):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)

    @pytest.mark.parametrize(
        ('name', 'dbos_settings'),
        [
            pytest.param(':memory:', {'SYSTEM_DATABASE_URL': 'sqlite:///:memory:'}, id='explicit'),
            pytest.param(':memory:', {}, id='derived'),
            pytest.param('file:memorydb_default?mode=memory&cache=shared', {}, id='derived-from-a-test-database'),
        ],
    )
    def test_in_memory_database_reports_a_warning(self, settings, mocker, name, dbos_settings):
        # Django renames the test database once a test uses it: pin the default database.
        in_memory = {'ENGINE': 'django.db.backends.sqlite3', 'NAME': name}
        mocker.patch.object(settings_module, '_django_database_settings', return_value=in_memory)
        set_dbos_options(settings, dbos_settings)

        warnings = check_in_memory_system_database()

        assert len(warnings) == 1
        assert warnings[0].id == 'django_dbos.W002'

    @pytest.mark.parametrize('url', ['sqlite:///db.sqlite3-dbos.sqlite', 'postgresql://db/dbos_app'])
    def test_shared_database_reports_nothing(self, settings, url):
        set_dbos_options(settings, {'SYSTEM_DATABASE_URL': url})
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
        set_dbos_options(settings, {})
        assert [message.id for message in check_serializer()] == ['django_dbos.W003']
        assert [message.id for message in check_executor_id()] == ['django_dbos.W004']

    def test_configured_deployment_reports_nothing(self, settings):
        set_dbos_options(settings, {'SERIALIZER': 'dbos.DBOSPortableJSONSerializer', 'EXECUTOR_ID': 'worker-1'})
        assert check_serializer() == []
        assert check_executor_id() == []

    def test_executor_id_from_the_environment(self, settings, monkeypatch):
        set_dbos_options(settings, {})
        monkeypatch.setenv('DBOS__VMID', 'machine-1')
        assert check_executor_id() == []


@pytest.mark.usefixtures('keep_known_tasks')
class TestCheckPartitionedQueueTasks:
    def test_tasks_of_the_test_suite_report_nothing(self):
        # greet_user targets the partitioned queue with a partition_key; the others use plain queues.
        assert check_partitioned_queue_tasks() == []

    @pytest.mark.parametrize(
        ('build', 'problem'),
        [
            pytest.param(lambda: test_tasks.add.using(queue_name='per_user'), 'it has no partition_key', id='no-key'),
            pytest.param(
                lambda: dbos_params(partition_key='user_id', deduplication_id='dedup')(test_tasks.greet_user),
                'it has a deduplication_id',
                id='deduplication',
            ),
        ],
    )
    def test_misconfigured_task_reports_an_error(self, mocker, build, problem):
        mocker.patch.object(checks, 'known_tasks', return_value=(build(),))

        errors = check_partitioned_queue_tasks()

        assert [error.id for error in errors] == ['django_dbos.E006']
        assert errors[0].msg.startswith("Task 'tests.tasks.")
        assert f"targets the partitioned queue 'per_user', but {problem}" in errors[0].msg

    def test_cron_task_reports_an_error(self, mocker):
        mocker.patch.object(checks, 'cron_tasks', return_value=(test_tasks.greet_user,))
        errors = check_partitioned_queue_tasks()
        assert [error.id for error in errors] == ['django_dbos.E006']
        assert 'scheduled with @cron' in errors[0].msg

    def test_each_problem_is_reported(self, mocker):
        # No partition_key, a deduplication_id and @cron: one error each, not just the first.
        task = dbos_params(deduplication_id='dedup')(test_tasks.add).using(queue_name='per_user')
        mocker.patch.object(checks, 'known_tasks', return_value=(task,))
        mocker.patch.object(checks, 'cron_tasks', return_value=(task,))

        errors = check_partitioned_queue_tasks()

        assert [error.id for error in errors] == ['django_dbos.E006'] * 3
        assert 'it has no partition_key' in errors[0].msg
        assert 'it has a deduplication_id' in errors[1].msg
        assert 'it is scheduled with @cron' in errors[2].msg

    def test_invalid_queues_are_left_to_e001(self, settings, monkeypatch):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        set_dbos_options(settings, {'QUEUES': ['default']})  # OPTIONS["QUEUES"] must be a mapping
        assert check_partitioned_queue_tasks() == []
        assert [error.id for error in check_dbos_settings()] == ['django_dbos.E001']


class TestCheckStepOptionsWithDurableOperations:
    def test_default_step_options_report_nothing(self, mocker):
        task = dbos_params(durable_operations=True)(test_tasks.add)
        mocker.patch.object(checks, 'known_tasks', return_value=(task,))
        assert check_step_options_with_durable_operations() == []

    def test_step_options_without_durable_operations_report_nothing(self, mocker):
        mocker.patch.object(checks, 'known_tasks', return_value=(test_tasks.flaky_with_retries,))
        assert check_step_options_with_durable_operations() == []

    def test_step_options_with_durable_operations_report_a_warning(self, mocker):
        task = dbos_params(durable_operations=True, step_retries_allowed=True, step_max_attempts=5)(test_tasks.add)
        mocker.patch.object(checks, 'known_tasks', return_value=(task,))

        warnings = check_step_options_with_durable_operations()

        assert [warning.id for warning in warnings] == ['django_dbos.W006']
        assert "Task 'tests.tasks.add' sets step_retries_allowed, step_max_attempts" in warnings[0].msg
        assert 'step_options' in warnings[0].hint


class TestCheckDbosBackendConfigured:
    def test_a_dbos_backend_reports_nothing(self):
        assert check_dbos_backend_configured() == []

    def test_no_dbos_backend_reports_a_warning(self, settings):
        settings.TASKS = {'default': {'BACKEND': 'django.tasks.backends.immediate.ImmediateBackend'}}

        warnings = check_dbos_backend_configured()

        assert [warning.id for warning in warnings] == ['django_dbos.W005']
        assert DBOS_BACKEND in warnings[0].hint
