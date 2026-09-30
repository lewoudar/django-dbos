"""Tests for the DJANGO_DBOS setting resolution."""

from types import SimpleNamespace

import pytest
from dbos import DBOSPortableJSONSerializer

from django_dbos import settings as dbos_settings
from django_dbos.checks import check_dbos_settings
from django_dbos.exceptions import ImproperlyConfiguredDbos, scrub_credentials


class TestSystemDatabaseUrlDerivation:
    @pytest.mark.parametrize(
        ('django_db', 'expected'),
        [
            pytest.param(
                {'ENGINE': 'django.db.backends.sqlite3', 'NAME': 'myproject.sqlite3'},
                'sqlite:///myproject.sqlite3-dbos.sqlite',
                id='sqlite-file',
            ),
            pytest.param(
                {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'},
                'sqlite:///:memory:',
                id='sqlite-memory',
            ),
            pytest.param(
                {
                    'ENGINE': 'django.db.backends.postgresql',
                    'NAME': 'myproject',
                    'USER': 'kevin',
                    'PASSWORD': 'hunter2',
                    'HOST': 'db.example.com',
                    'PORT': '5433',
                },
                'postgresql+psycopg://kevin:hunter2@db.example.com:5433/dbos_myproject',
                id='postgres-full',
            ),
            pytest.param(
                {
                    'ENGINE': 'django.db.backends.postgresql',
                    'NAME': 'myproject',
                    'USER': 'user@domain',
                    'PASSWORD': 'p@ss:word/',
                },
                'postgresql+psycopg://user%40domain:p%40ss%3Aword%2F@localhost:5432/dbos_myproject',
                id='postgres-percent-encoded',
            ),
            pytest.param(
                {'ENGINE': 'django.db.backends.postgresql', 'NAME': 'myproject'},
                'postgresql+psycopg://:@localhost:5432/dbos_myproject',
                id='postgres-defaults',
            ),
        ],
    )
    def test_derivation(self, django_db, expected):
        assert dbos_settings.derive_system_database_url(django_db) == expected

    def test_postgres_custom_prefix(self, settings):
        settings.DJANGO_DBOS = {'SYSTEM_DB_PREFIX': 'mydbos_'}
        url = dbos_settings.derive_system_database_url({'ENGINE': 'django.db.backends.postgresql', 'NAME': 'myproject'})
        assert url == 'postgresql+psycopg://:@localhost:5432/mydbos_myproject'

    def test_unsupported_engine_raises(self):
        with pytest.raises(ImproperlyConfiguredDbos):
            dbos_settings.derive_system_database_url({'ENGINE': 'django.db.backends.oracle', 'NAME': 'orcl'})


class TestSystemDatabaseUrlResolution:
    def test_env_var_wins(self, settings, monkeypatch):
        monkeypatch.setenv('DBOS_SYSTEM_DATABASE_URL', 'postgresql://env-url')
        assert dbos_settings.resolve_system_database_url() == 'postgresql://env-url'

    def test_setting_wins(self, settings, monkeypatch):
        monkeypatch.setenv('DBOS_SYSTEM_DATABASE_URL', 'postgresql://env-url')
        settings.DJANGO_DBOS = {'SYSTEM_DATABASE_URL': 'postgresql://setting-url'}
        assert dbos_settings.resolve_system_database_url() == 'postgresql://setting-url'

    def test_falls_back_to_derivation(self, settings, monkeypatch):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        settings.DJANGO_DBOS = {'SYSTEM_DATABASE_URL': None}
        # The derived URL follows the configured Django database (pytest-django
        # may rewrite the test database name, so assert on the derivation rule).
        url = dbos_settings.resolve_system_database_url()
        assert url.startswith('sqlite:///')
        assert url.endswith('-dbos.sqlite') or url == 'sqlite:///:memory:'

    @pytest.mark.parametrize(
        'default_db',
        [
            pytest.param({'ENGINE': 'django.db.backends.dummy'}, id='dummy-engine'),
            pytest.param({'ENGINE': 'django.db.backends.sqlite3'}, id='no-name'),
        ],
    )
    def test_no_usable_django_database_raises(self, settings, monkeypatch, mocker, default_db):
        # Patch the module-level seam, never django.conf.settings.DATABASES.
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        settings.DJANGO_DBOS = {'SYSTEM_DATABASE_URL': None}
        mocker.patch.object(dbos_settings, 'settings', SimpleNamespace(DATABASES={'default': default_db}))
        with pytest.raises(ImproperlyConfiguredDbos, match='No default Django database is configured'):
            dbos_settings.resolve_system_database_url()


class TestQueues:
    def test_default_queue(self, settings):
        settings.DJANGO_DBOS = {}
        queues = dbos_settings.resolve_queues()
        assert [q.name for q in queues] == ['default']

    def test_declared_queues(self, settings):
        settings.DJANGO_DBOS = {
            'QUEUES': {
                'emails': {'worker_concurrency': 4},
                'reports': {'global_concurrency': 2},
            },
        }
        queues = dbos_settings.resolve_queues()
        assert [q.name for q in queues] == ['emails', 'reports']
        assert queues[0].worker_concurrency == 4
        assert queues[1].global_concurrency == 2

    def test_non_mapping_queues_raise(self, settings):
        settings.DJANGO_DBOS = {'QUEUES': ['emails']}
        with pytest.raises(ImproperlyConfiguredDbos):
            dbos_settings.resolve_queues()

    def test_names_colliding_once_stringified_raise(self, settings):
        settings.DJANGO_DBOS = {'QUEUES': {1: None, '1': None}}
        with pytest.raises(ImproperlyConfiguredDbos, match='Duplicate queue names'):
            dbos_settings.resolve_queues()

    def test_non_mapping_queue_capacity_raises(self, settings):
        settings.DJANGO_DBOS = {'QUEUES': {'emails': {'capacity': 5}}}
        with pytest.raises(ImproperlyConfiguredDbos, match="\\['emails'\\]\\['capacity'\\] must be a mapping"):
            dbos_settings.resolve_queues()


class TestAppName:
    def test_defaults_to_django(self, settings):
        settings.DJANGO_DBOS = {}
        assert dbos_settings.resolve_app_name() == 'django'

    def test_from_setting(self, settings):
        settings.DJANGO_DBOS = {'NAME': 'myproject'}
        assert dbos_settings.resolve_app_name() == 'myproject'

    def test_resolve_settings(self, settings, monkeypatch):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        settings.DJANGO_DBOS = {
            'NAME': 'myproject',
            'APPLICATION_VERSION': '1.2.3',
            'SYSTEM_DB_SCHEMA': 'dbos_custom',
        }
        resolved = dbos_settings.resolve_settings()
        assert resolved.app_name == 'myproject'
        assert resolved.application_version == '1.2.3'
        assert resolved.dbos_system_schema == 'dbos_custom'
        assert [q.name for q in resolved.queues] == ['default']


class TestCredentialScrubbing:
    def test_postgres_dsn_password_masked(self):
        message = 'connection to server failed: postgresql+psycopg://app_user:s3cret-pw@db.internal:5432/dbos_mydb'
        scrubbed = scrub_credentials(message)
        assert 's3cret-pw' not in scrubbed
        assert 'postgresql+psycopg://app_user:***@db.internal:5432/dbos_mydb' in scrubbed

    def test_schemes_and_special_characters(self):
        scrubbed = scrub_credentials('mysql://u:p@h/db then postgresql://u2:p2%40x@h2/db2')
        assert 'mysql://u:***@h/db' in scrubbed
        assert 'postgresql://u2:***@h2/db2' in scrubbed

    def test_message_without_dsn_unchanged(self):
        message = 'No credentials in here, just text.'
        assert scrub_credentials(message) == message


class TestQueueOptionsValidation:
    def test_unknown_option_is_reported_with_the_valid_ones(self, settings):
        settings.DJANGO_DBOS = {'QUEUES': {'emails': {'worker_concurency': 2}}}
        with pytest.raises(ImproperlyConfiguredDbos) as excinfo:
            dbos_settings.resolve_queues()
        message = str(excinfo.value)
        assert "QUEUES']['emails']" in message
        assert 'worker_concurency' in message
        assert 'worker_concurrency' in message and 'limiter' in message

    def test_non_mapping_options_are_rejected(self, settings):
        settings.DJANGO_DBOS = {'QUEUES': {'emails': 4}}
        with pytest.raises(ImproperlyConfiguredDbos, match=r"QUEUES'\]\['emails'\] must be a mapping"):
            dbos_settings.resolve_queues()

    def test_none_means_no_options(self, settings):
        settings.DJANGO_DBOS = {'QUEUES': {'emails': None, 'reports': {'worker_concurrency': 1}}}
        emails, reports = dbos_settings.resolve_queues()
        assert emails.worker_concurrency is None
        assert reports.worker_concurrency == 1

    def test_reported_by_the_system_check(self, settings):
        settings.DJANGO_DBOS = {'SYSTEM_DATABASE_URL': 'postgresql://db', 'QUEUES': {'emails': {'nope': 1}}}
        errors = check_dbos_settings()
        assert [error.id for error in errors] == ['django_dbos.E001']
        assert 'nope' in errors[0].msg


class TestSerializerResolution:
    def test_unset_keeps_dbos_default(self, settings):
        settings.DJANGO_DBOS = {}
        assert dbos_settings.resolve_serializer() is None

    @pytest.mark.parametrize(
        'value',
        [
            pytest.param('dbos.DBOSPortableJSONSerializer', id='dotted-path-to-class'),
            pytest.param('dbos.DBOSPortableJSONSerializer', id='class'),
        ],
    )
    def test_class_or_path_is_instantiated(self, settings, value):
        settings.DJANGO_DBOS = {'SERIALIZER': DBOSPortableJSONSerializer if value == 'class' else value}
        assert isinstance(dbos_settings.resolve_serializer(), DBOSPortableJSONSerializer)

    @pytest.mark.parametrize('name', ['MsgspecJSONSerializer', 'MsgspecMsgpackSerializer'])
    def test_msgspec_serializers_resolve_from_their_path(self, settings, name):
        settings.DJANGO_DBOS = {'SERIALIZER': f'django_dbos.serializers.{name}'}
        assert type(dbos_settings.resolve_serializer()).__name__ == name

    def test_instance_is_used_as_is(self, settings):
        instance = DBOSPortableJSONSerializer()
        settings.DJANGO_DBOS = {'SERIALIZER': instance}
        assert dbos_settings.resolve_serializer() is instance

    @pytest.mark.parametrize(
        'value',
        [
            pytest.param('nowhere.NoSerializer', id='unknown-path'),
            pytest.param('json.dumps', id='not-a-serializer'),
            pytest.param(object(), id='random-object'),
        ],
    )
    def test_invalid_values_are_rejected(self, settings, value):
        settings.DJANGO_DBOS = {'SERIALIZER': value}
        with pytest.raises(ImproperlyConfiguredDbos, match='SERIALIZER'):
            dbos_settings.resolve_serializer()
