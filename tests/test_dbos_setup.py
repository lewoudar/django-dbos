"""Tests for dbos_setup helpers: config building and queue registration."""

from django_dbos import dbos_setup


class TestBuildDbosConfig:
    def test_minimal_config(self, settings, monkeypatch):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        settings.DJANGO_DBOS = {'SYSTEM_DATABASE_URL': 'postgresql://db'}
        config = dbos_setup._build_dbos_config()
        assert config['name'] == 'django'
        assert config['system_database_url'] == 'postgresql://db'
        assert 'application_version' not in config
        assert 'dbos_system_schema' not in config
        assert 'scheduler_polling_interval_sec' not in config

    def test_full_config(self, settings, monkeypatch):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        settings.DJANGO_DBOS = {
            'NAME': 'myproject',
            'SYSTEM_DATABASE_URL': 'postgresql://db',
            'APPLICATION_VERSION': '1.2.3',
            'SYSTEM_DB_SCHEMA': 'custom_schema',
            'SCHEDULER_POLLING_INTERVAL_SEC': 2,
        }
        config = dbos_setup._build_dbos_config()
        assert config['name'] == 'myproject'
        assert config['application_version'] == '1.2.3'
        assert config['dbos_system_schema'] == 'custom_schema'
        assert config['scheduler_polling_interval_sec'] == 2.0


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
