"""Tests for dbos_setup helpers: config building, queue registration and launch ordering."""

import pytest

from django_dbos import dbos_setup
from django_dbos.exceptions import ImproperlyConfiguredDbos


class TestBuildDbosConfig:
    def test_minimal_config(self, settings, monkeypatch):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        monkeypatch.delenv('DBOS__VMID', raising=False)
        settings.DJANGO_DBOS = {'SYSTEM_DATABASE_URL': 'postgresql://db'}
        config = dbos_setup._build_dbos_config()
        assert config['name'] == 'django'
        assert config['system_database_url'] == 'postgresql://db'
        assert 'application_version' not in config
        assert 'dbos_system_schema' not in config
        assert 'executor_id' not in config
        assert 'serializer' not in config
        assert 'run_migrations' not in config
        assert 'scheduler_polling_interval_sec' not in config

    def test_full_config(self, settings, monkeypatch):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        settings.DJANGO_DBOS = {
            'NAME': 'myproject',
            'SYSTEM_DATABASE_URL': 'postgresql://db',
            'APPLICATION_VERSION': '1.2.3',
            'SYSTEM_DB_SCHEMA': 'custom_schema',
            'EXECUTOR_ID': 'machine-42',
            'SERIALIZER': 'dbos.DBOSPortableJSONSerializer',
            'RUN_MIGRATIONS': False,
            'SCHEDULER_POLLING_INTERVAL_SEC': 2,
        }
        config = dbos_setup._build_dbos_config()
        assert config['name'] == 'myproject'
        assert config['application_version'] == '1.2.3'
        assert config['dbos_system_schema'] == 'custom_schema'
        assert config['executor_id'] == 'machine-42'
        assert type(config['serializer']).__name__ == 'DBOSPortableJSONSerializer'
        assert config['run_migrations'] is False
        assert config['scheduler_polling_interval_sec'] == 2.0

    def test_executor_id_falls_back_to_dbos_env(self, settings, monkeypatch):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        monkeypatch.setenv('DBOS__VMID', 'vm-7')
        settings.DJANGO_DBOS = {'SYSTEM_DATABASE_URL': 'postgresql://db'}
        assert dbos_setup._build_dbos_config()['executor_id'] == 'vm-7'


class TestRegisterQueues:
    def test_all_queue_options_forwarded(self, settings, mocker):
        queue_cls = mocker.patch('dbos.Queue')
        settings.DJANGO_DBOS = {
            'QUEUES': {
                'emails': {
                    'worker_concurrency': 2,
                    'global_concurrency': 3,
                    'limiter': {'strategy': 'fixed_window', 'limit': 10, 'seconds': 60},
                    'polling_interval_sec': 1.5,
                }
            },
        }
        dbos_setup._register_queues()
        queue_cls.assert_called_once_with(
            'emails',
            worker_concurrency=2,
            global_concurrency=3,
            limiter={'strategy': 'fixed_window', 'limit': 10, 'seconds': 60},
            polling_interval_sec=1.5,
        )

    def test_default_queue_has_no_options(self, settings, mocker):
        queue_cls = mocker.patch('dbos.Queue')
        settings.DJANGO_DBOS = {}
        dbos_setup._register_queues()
        queue_cls.assert_called_once_with('default')


class TestScrubbedSdkErrors:
    DSN_ERROR = RuntimeError('could not connect to postgresql://user:hunter2@db:5432/dbos_app')

    def test_initialization_failure_is_scrubbed_and_unchained(self, settings, monkeypatch, mocker):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        settings.DJANGO_DBOS = {'SYSTEM_DATABASE_URL': 'postgresql://user:hunter2@db:5432/dbos_app'}
        mocker.patch.object(dbos_setup, '_initialized', False)
        mocker.patch.object(dbos_setup, 'DBOS', side_effect=self.DSN_ERROR)

        with pytest.raises(ImproperlyConfiguredDbos) as excinfo:
            dbos_setup.ensure_initialized()

        message = str(excinfo.value)
        assert message.startswith('RuntimeError: ')
        assert 'hunter2' not in message
        assert 'user:***@db:5432' in message
        assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__
        assert dbos_setup._initialized is False

    def test_launch_failure_is_scrubbed_and_unchained(self, mocker):
        mocker.patch.object(dbos_setup, '_initialized', True)
        mocker.patch.object(dbos_setup, '_launched', False)
        mocker.patch('django_dbos.tasks_registry.register_all_task_workflows')
        mocker.patch.object(dbos_setup.DBOS, 'launch', side_effect=self.DSN_ERROR)

        with pytest.raises(ImproperlyConfiguredDbos) as excinfo:
            dbos_setup.ensure_launched()

        assert 'hunter2' not in str(excinfo.value)
        assert excinfo.value.__cause__ is None
        assert dbos_setup._launched is False

    def test_configuration_errors_pass_through(self, settings, monkeypatch, mocker):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        settings.DJANGO_DBOS = {'SYSTEM_DATABASE_URL': 'postgresql://db', 'QUEUES': {'emails': {'nope': 1}}}
        mocker.patch.object(dbos_setup, '_initialized', False)
        mocker.patch.object(dbos_setup, 'DBOS')

        with pytest.raises(ImproperlyConfiguredDbos, match='unknown option'):
            dbos_setup.ensure_initialized()


class TestEnsureLaunched:
    def test_registers_known_tasks_before_launch(self, mocker):
        # The worker must know every task before it starts dequeuing.
        mocker.patch.object(dbos_setup, '_initialized', True)
        mocker.patch.object(dbos_setup, '_launched', False)
        calls: list[str] = []
        mocker.patch(
            'django_dbos.tasks_registry.register_all_task_workflows',
            side_effect=lambda: calls.append('register'),
        )
        mocker.patch.object(dbos_setup.DBOS, 'launch', side_effect=lambda: calls.append('launch'))

        dbos_setup.ensure_launched()

        assert calls == ['register', 'launch']
        assert dbos_setup._launched is True
