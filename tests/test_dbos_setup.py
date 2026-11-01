"""Tests for dbos_setup helpers: config building, queue registration and launch ordering."""

import pytest

from django_dbos import dbos_setup
from django_dbos.exceptions import ImproperlyConfiguredDbos
from tests.helpers import set_dbos_options


class TestBuildDbosConfig:
    def test_minimal_config(self, settings, monkeypatch):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        monkeypatch.delenv('DBOS__VMID', raising=False)
        monkeypatch.delenv('DBOS_APPLICATION_NAME', raising=False)
        set_dbos_options(settings, {'SYSTEM_DATABASE_URL': 'postgresql://db'})
        config = dbos_setup._build_dbos_config()
        assert config['name'] == 'django'
        assert config['system_database_url'] == 'postgresql://db'
        assert 'application_version' not in config
        assert 'dbos_system_schema' not in config
        assert 'executor_id' not in config
        assert 'serializer' not in config
        assert config['run_migrations'] is True  # DBOS' own default, made explicit
        assert 'scheduler_polling_interval_sec' not in config

    def test_full_config(self, settings, monkeypatch):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        set_dbos_options(
            settings,
            {
                'NAME': 'myproject',
                'SYSTEM_DATABASE_URL': 'postgresql://db',
                'APPLICATION_VERSION': '1.2.3',
                'SYSTEM_DB_SCHEMA': 'custom_schema',
                'EXECUTOR_ID': 'machine-42',
                'SERIALIZER': 'dbos.DBOSPortableJSONSerializer',
                'RUN_MIGRATIONS': False,
                'SCHEDULER_POLLING_INTERVAL_SEC': 2,
            },
        )
        config = dbos_setup._build_dbos_config()
        assert config['name'] == 'myproject'
        assert config['system_database_url'] == 'postgresql://db'
        assert config['application_version'] == '1.2.3'
        assert config['dbos_system_schema'] == 'custom_schema'
        assert config['executor_id'] == 'machine-42'
        assert type(config['serializer']).__name__ == 'DBOSPortableJSONSerializer'
        assert config['run_migrations'] is False
        assert config['scheduler_polling_interval_sec'] == 2.0


class TestRegisterQueues:
    def test_all_queue_options_forwarded(self, settings, mocker):
        register_queue = mocker.patch.object(dbos_setup.DBOS, 'register_queue')
        set_dbos_options(
            settings,
            {
                'QUEUES': {
                    'emails': {
                        'worker_concurrency': 2,
                        'global_concurrency': 3,
                        'limiter': {'strategy': 'fixed_window', 'limit': 10, 'seconds': 60},
                        'polling_interval_sec': 1.5,
                    }
                },
            },
        )
        dbos_setup._register_queues()
        register_queue.assert_called_once_with(
            'emails',
            worker_concurrency=2,
            global_concurrency=3,
            limiter={'strategy': 'fixed_window', 'limit': 10, 'seconds': 60},
            polling_interval_sec=1.5,
        )

    def test_partition_options_forwarded(self, settings, mocker):
        register_queue = mocker.patch.object(dbos_setup.DBOS, 'register_queue')
        partition_options = {
            'partition_concurrency': 2,
            'partition_worker_concurrency': 1,
            'partition_limiter': {'limit': 5, 'period': 60},
        }
        set_dbos_options(settings, {'QUEUES': {'per_user': partition_options}})
        dbos_setup._register_queues()
        register_queue.assert_called_once_with('per_user', **partition_options)

    def test_default_queue_has_no_options(self, settings, mocker):
        register_queue = mocker.patch.object(dbos_setup.DBOS, 'register_queue')
        set_dbos_options(settings, {})
        dbos_setup._register_queues()
        register_queue.assert_called_once_with('default')


class TestScrubbedSdkErrors:
    DSN_ERROR = RuntimeError('could not connect to postgresql://user:hunter2@db:5432/dbos_app')

    def test_initialization_failure_is_scrubbed_and_unchained(self, settings, monkeypatch, mocker):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        set_dbos_options(settings, {'SYSTEM_DATABASE_URL': 'postgresql://user:hunter2@db:5432/dbos_app'})
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
        mocker.patch('django_dbos.dbos_setup.register_all_task_workflows')
        mocker.patch.object(dbos_setup.DBOS, 'launch', side_effect=self.DSN_ERROR)

        with pytest.raises(ImproperlyConfiguredDbos) as excinfo:
            dbos_setup.ensure_launched()

        assert 'hunter2' not in str(excinfo.value)
        assert excinfo.value.__cause__ is None
        assert dbos_setup._launched is False

    def test_configuration_errors_pass_through(self, settings, monkeypatch, mocker):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        set_dbos_options(settings, {'SYSTEM_DATABASE_URL': 'postgresql://db', 'QUEUES': {'emails': {'nope': 1}}})
        mocker.patch.object(dbos_setup, '_initialized', False)
        mocker.patch.object(dbos_setup, 'DBOS')

        with pytest.raises(ImproperlyConfiguredDbos, match='unknown option'):
            dbos_setup.ensure_initialized()


class TestEnsureLaunched:
    def test_registers_known_tasks_before_launch(self, mocker):
        # The worker must know every task before it starts dequeuing; queues
        # persist to the system database, which only exists once launched.
        mocker.patch.object(dbos_setup, '_initialized', True)
        mocker.patch.object(dbos_setup, '_launched', False)
        calls: list[str] = []
        mocker.patch(
            'django_dbos.dbos_setup.register_all_task_workflows',
            side_effect=lambda: calls.append('register'),
        )
        # Every launched worker runs DBOS' scheduler and may dequeue a cron firing.
        mocker.patch(
            'django_dbos.dbos_setup.register_cron_workflows',
            side_effect=lambda: calls.append('cron'),
        )
        mocker.patch.object(dbos_setup.DBOS, 'launch', side_effect=lambda: calls.append('launch'))
        mocker.patch.object(dbos_setup, '_register_queues', side_effect=lambda: calls.append('queues'))
        listen_queues = mocker.patch.object(dbos_setup.DBOS, 'listen_queues')

        dbos_setup.ensure_launched()

        assert calls == ['register', 'cron', 'launch', 'queues']
        assert dbos_setup._launched is True
        listen_queues.assert_not_called()  # every queue

    def test_listens_to_the_selected_queues_before_launch(self, mocker):
        # DBOS refuses listen_queues once launched.
        mocker.patch.object(dbos_setup, '_initialized', True)
        mocker.patch.object(dbos_setup, '_launched', False)
        mocker.patch('django_dbos.dbos_setup.register_all_task_workflows')
        mocker.patch('django_dbos.dbos_setup.register_cron_workflows')
        mocker.patch.object(dbos_setup, '_register_queues')
        calls: list[object] = []
        mocker.patch.object(dbos_setup.DBOS, 'listen_queues', side_effect=calls.append)
        mocker.patch.object(dbos_setup.DBOS, 'launch', side_effect=lambda: calls.append('launch'))

        dbos_setup.ensure_launched(listen_queues=['emails'])

        assert calls == [['emails'], 'launch']

    def test_second_call_is_a_noop(self, mocker):
        mocker.patch.object(dbos_setup, '_initialized', True)
        mocker.patch.object(dbos_setup, '_launched', True)
        register = mocker.patch('django_dbos.dbos_setup.register_all_task_workflows')
        launch = mocker.patch.object(dbos_setup.DBOS, 'launch')

        dbos_setup.ensure_launched()

        register.assert_not_called()
        launch.assert_not_called()

    def test_queue_configuration_errors_pass_through(self, mocker):
        mocker.patch.object(dbos_setup, '_initialized', True)
        mocker.patch.object(dbos_setup, '_launched', False)
        mocker.patch('django_dbos.dbos_setup.register_all_task_workflows')
        mocker.patch.object(dbos_setup.DBOS, 'launch')
        error = ImproperlyConfiguredDbos('queue misconfigured')
        mocker.patch.object(dbos_setup, '_register_queues', side_effect=error)

        with pytest.raises(ImproperlyConfiguredDbos) as excinfo:
            dbos_setup.ensure_launched()

        assert excinfo.value is error
        assert dbos_setup._launched is False


class TestShutdown:
    def test_waits_for_in_flight_workflows_by_default(self, mocker):
        # DBOS.destroy() defaults to workflow_completion_timeout_sec=0 (no
        # wait), which races its own background threads: a step or workflow
        # still persisting its result can access DBOS._sys_db just after
        # shutdown tears it down. Give in-flight work a grace period.
        mocker.patch.object(dbos_setup, '_initialized', True)
        mocker.patch.object(dbos_setup, '_launched', True)
        destroy = mocker.patch.object(dbos_setup.DBOS, 'destroy')

        dbos_setup.shutdown()

        destroy.assert_called_once_with(workflow_completion_timeout_sec=10)
        assert dbos_setup._initialized is False
        assert dbos_setup._launched is False

    def test_timeout_is_overridable(self, mocker):
        mocker.patch.object(dbos_setup, '_initialized', True)
        mocker.patch.object(dbos_setup, '_launched', True)
        destroy = mocker.patch.object(dbos_setup.DBOS, 'destroy')

        dbos_setup.shutdown(workflow_completion_timeout_sec=30)

        destroy.assert_called_once_with(workflow_completion_timeout_sec=30)

    def test_noop_when_never_initialized(self, mocker):
        mocker.patch.object(dbos_setup, '_initialized', False)
        destroy = mocker.patch.object(dbos_setup.DBOS, 'destroy')

        dbos_setup.shutdown()

        destroy.assert_not_called()


class TestCockroachDbEngine:
    URL = 'cockroachdb+psycopg2://root@crdb:26257/dbos'

    @pytest.fixture(autouse=True)
    def cockroachdb(self, settings, monkeypatch, mocker):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        set_dbos_options(settings, {'SYSTEM_DATABASE_URL': self.URL})
        mocker.patch.object(dbos_setup, '_initialized', False)
        mocker.patch.object(dbos_setup, '_launched', False)

    def test_engine_is_handed_to_dbos_and_disposed_on_shutdown(self, mocker):
        dbos_cls = mocker.patch.object(dbos_setup, 'DBOS')

        dbos_setup.ensure_initialized()

        config = dbos_cls.call_args.kwargs['config']
        assert config['system_database_url'] == self.URL
        assert config['use_listen_notify'] is False
        dispose = mocker.spy(config['system_database_engine'], 'dispose')
        dbos_setup.shutdown()
        dispose.assert_called_once_with()
        assert dbos_setup._engine_options == {}

    @pytest.mark.parametrize(
        ('error', 'message'),
        [
            pytest.param(RuntimeError('boom'), r'^RuntimeError: boom$', id='wrapped'),
            pytest.param(ImproperlyConfiguredDbos('bad'), r'^bad$', id='passed-through'),
        ],
    )
    def test_engine_is_disposed_when_initialization_fails(self, mocker, error, message):
        engine = mocker.Mock()
        mocker.patch.object(
            dbos_setup.system_engine,
            'system_database_options',
            return_value={'system_database_engine': engine, 'use_listen_notify': False},
        )
        mocker.patch.object(dbos_setup, 'DBOS', side_effect=error)

        with pytest.raises(ImproperlyConfiguredDbos, match=message):
            dbos_setup.ensure_initialized()

        engine.dispose.assert_called_once_with()
        assert dbos_setup._engine_options == {}
