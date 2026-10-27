"""Tests for the resolution of the DBOS backend's OPTIONS."""

import re
from types import SimpleNamespace

import pytest
from dbos import DBOSPortableJSONSerializer

from django_dbos import settings as dbos_settings
from django_dbos.checks import check_dbos_settings
from django_dbos.exceptions import ImproperlyConfiguredDbos, scrub_credentials
from tests.helpers import set_dbos_options

DBOS_BACKEND = 'django_dbos.backends.DBOSBackend'


class TestDbosBackendOptions:
    def test_options_of_the_dbos_backend(self, settings):
        set_dbos_options(settings, {'NAME': 'myproject'})
        assert dbos_settings.get_dbos_options() == {'NAME': 'myproject'}
        assert dbos_settings.get_dbos_setting('NAME') == 'myproject'
        assert dbos_settings.options_path('NAME') == "TASKS['dbos']['OPTIONS']['NAME']"

    def test_no_options_on_the_backend(self, settings):
        settings.TASKS = {'tasks': {'BACKEND': DBOS_BACKEND}}
        assert dbos_settings.get_dbos_options() == {}
        assert dbos_settings.options_path() == "TASKS['tasks']['OPTIONS']"

    def test_without_a_dbos_backend_everything_defaults(self, settings):
        settings.TASKS = {'default': {'BACKEND': 'django.tasks.backends.immediate.ImmediateBackend'}}
        assert dbos_settings.get_dbos_setting('NAME', 'fallback') == 'fallback'
        assert dbos_settings.options_path('NAME') == "TASKS[<alias>]['OPTIONS']['NAME']"
        assert [queue.name for queue in dbos_settings.resolve_queues()] == ['default']

    @pytest.mark.parametrize('options', [['NAME'], 'myproject'], ids=['list', 'string'])
    def test_non_mapping_options_read_as_empty_but_are_reported(self, settings, monkeypatch, options):
        # the admin and the other checks keep working; E001, the client and the worker refuse them
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        settings.TASKS = {'dbos': {'BACKEND': DBOS_BACKEND, 'OPTIONS': options}}
        assert dbos_settings.get_dbos_options() == {}
        assert dbos_settings.get_dbos_setting('NAME', 'fallback') == 'fallback'
        with pytest.raises(ImproperlyConfiguredDbos, match=r"^TASKS\['dbos'\]\['OPTIONS'\] must be a mapping"):
            dbos_settings.resolve_settings()
        assert [error.id for error in check_dbos_settings()] == ['django_dbos.E001']

    def test_the_first_dbos_backend_wins(self, settings):
        # several are reported by django_dbos.E002
        settings.TASKS = {
            'first': {'BACKEND': DBOS_BACKEND, 'OPTIONS': {'NAME': 'first'}},
            'second': {'BACKEND': DBOS_BACKEND, 'OPTIONS': {'NAME': 'second'}},
        }
        assert dbos_settings.get_dbos_setting('NAME') == 'first'


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
            pytest.param(
                {'ENGINE': 'django_cockroachdb', 'NAME': 'myproject', 'USER': 'kevin', 'PASSWORD': 'hunter2'},
                'cockroachdb+psycopg2://kevin:hunter2@localhost:26257/dbos_myproject',
                id='cockroachdb-defaults',
            ),
            pytest.param(
                {'ENGINE': 'django_cockroachdb', 'NAME': 'myproject', 'HOST': 'crdb.example.com', 'PORT': 26000},
                'cockroachdb+psycopg2://:@crdb.example.com:26000/dbos_myproject',
                id='cockroachdb-full',
            ),
        ],
    )
    def test_derivation(self, django_db, expected):
        assert dbos_settings.derive_system_database_url(django_db) == expected

    def test_postgres_custom_prefix(self, settings):
        set_dbos_options(settings, {'SYSTEM_DB_PREFIX': 'mydbos_'})
        url = dbos_settings.derive_system_database_url({'ENGINE': 'django.db.backends.postgresql', 'NAME': 'myproject'})
        assert url == 'postgresql+psycopg://:@localhost:5432/mydbos_myproject'

    def test_unsupported_engine_raises(self):
        with pytest.raises(
            ImproperlyConfiguredDbos, match=r"^Unsupported database engine 'django\.db\.backends\.oracle'"
        ):
            dbos_settings.derive_system_database_url({'ENGINE': 'django.db.backends.oracle', 'NAME': 'orcl'})


class TestSystemDatabaseUrlResolution:
    def test_env_var_wins(self, settings, monkeypatch):
        monkeypatch.setenv('DBOS_SYSTEM_DATABASE_URL', 'postgresql://env-url')
        assert dbos_settings.resolve_system_database_url() == 'postgresql://env-url'

    def test_setting_wins(self, settings, monkeypatch):
        monkeypatch.setenv('DBOS_SYSTEM_DATABASE_URL', 'postgresql://env-url')
        set_dbos_options(settings, {'SYSTEM_DATABASE_URL': 'postgresql://setting-url'})
        assert dbos_settings.resolve_system_database_url() == 'postgresql://setting-url'

    def test_falls_back_to_derivation(self, settings, monkeypatch):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        set_dbos_options(settings, {'SYSTEM_DATABASE_URL': None})
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
        set_dbos_options(settings, {'SYSTEM_DATABASE_URL': None})
        mocker.patch.object(dbos_settings, 'settings', SimpleNamespace(DATABASES={'default': default_db}))
        with pytest.raises(ImproperlyConfiguredDbos, match='No default Django database is configured'):
            dbos_settings.resolve_system_database_url()

    @pytest.mark.parametrize('url', ['cockroachdb://root@crdb:26257/dbos', 'cockroachdb+psycopg2://root@crdb/dbos'])
    def test_cockroachdb_psycopg2_url_is_kept(self, settings, url):
        set_dbos_options(settings, {'SYSTEM_DATABASE_URL': url})
        assert dbos_settings.resolve_system_database_url() == url
        assert dbos_settings.is_cockroachdb_url(url)

    @pytest.mark.parametrize('driver', ['cockroachdb+psycopg', 'cockroachdb+asyncpg'])
    def test_cockroachdb_url_with_another_driver_raises(self, settings, driver):
        set_dbos_options(settings, {'SYSTEM_DATABASE_URL': f'{driver}://root@crdb:26257/dbos'})
        with pytest.raises(ImproperlyConfiguredDbos, match=rf"^The '{re.escape(driver)}' driver cannot reach"):
            dbos_settings.resolve_system_database_url()

    def test_postgres_url_is_not_cockroachdb(self):
        assert not dbos_settings.is_cockroachdb_url('postgresql+psycopg://db/dbos')


class TestBooleanOptions:
    @pytest.mark.parametrize('key', list(dbos_settings.BOOLEAN_OPTIONS))
    def test_unset_reads_the_default(self, settings, key):
        set_dbos_options(settings, {})
        assert dbos_settings.get_bool_option(key) is True

    @pytest.mark.parametrize('value', [True, False])
    def test_a_boolean_is_kept(self, settings, value):
        set_dbos_options(settings, {'RUN_MIGRATIONS': value})
        assert dbos_settings.get_bool_option('RUN_MIGRATIONS') is value

    @pytest.mark.parametrize(
        'value', [pytest.param('false', id='string'), pytest.param(0, id='zero'), pytest.param(1, id='one')]
    )
    def test_anything_else_raises(self, settings, value):
        set_dbos_options(settings, {'MIGRATE_WITH_DJANGO': value})
        with pytest.raises(ImproperlyConfiguredDbos) as excinfo:
            dbos_settings.get_bool_option('MIGRATE_WITH_DJANGO')
        assert str(excinfo.value) == (
            f"TASKS['dbos']['OPTIONS']['MIGRATE_WITH_DJANGO'] must be True or False, got {value!r}."
        )

    @pytest.mark.parametrize('value', [None, True, 'false'])
    def test_an_unknown_key_is_a_bug_whatever_the_settings(self, settings, value):
        set_dbos_options(settings, {'SERIALIZER': value})
        with pytest.raises(KeyError):
            dbos_settings.get_bool_option('SERIALIZER')

    @pytest.mark.parametrize('key', list(dbos_settings.BOOLEAN_OPTIONS))
    def test_an_invalid_value_is_reported_by_e001(self, settings, monkeypatch, key):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        set_dbos_options(settings, {key: 'false'})
        errors = check_dbos_settings()
        assert [error.id for error in errors] == ['django_dbos.E001']
        assert f"['{key}'] must be True or False" in errors[0].msg


class TestQueues:
    def test_default_queue(self, settings):
        set_dbos_options(settings, {})
        queues = dbos_settings.resolve_queues()
        assert [q.name for q in queues] == ['default']

    def test_declared_queues(self, settings):
        set_dbos_options(
            settings,
            {
                'QUEUES': {
                    'emails': {'worker_concurrency': 4},
                    'reports': {'global_concurrency': 2},
                },
            },
        )
        queues = dbos_settings.resolve_queues()
        assert [q.name for q in queues] == ['emails', 'reports']
        assert queues[0].worker_concurrency == 4
        assert queues[1].global_concurrency == 2

    @pytest.mark.parametrize('queues', [['emails'], {}], ids=['list', 'empty'])
    def test_options_queues_must_be_a_non_empty_mapping(self, settings, queues):
        set_dbos_options(settings, {'QUEUES': queues})
        with pytest.raises(ImproperlyConfiguredDbos, match=r"^TASKS\['dbos'\]\['OPTIONS'\]\['QUEUES'\] must be"):
            dbos_settings.resolve_queues()

    def test_top_level_list_declares_queues_without_options(self, settings):
        settings.TASKS = {'dbos': {'BACKEND': DBOS_BACKEND, 'QUEUES': ['default', 'emails']}}
        assert dbos_settings.resolve_queues() == (
            dbos_settings.QueueConfig(name='default'),
            dbos_settings.QueueConfig(name='emails'),
        )

    def test_queues_declared_twice_raise(self, settings):
        settings.TASKS = {
            'dbos': {'BACKEND': DBOS_BACKEND, 'QUEUES': ['default'], 'OPTIONS': {'QUEUES': {'default': None}}}
        }
        with pytest.raises(ImproperlyConfiguredDbos, match=r"either in TASKS\['dbos'\]\['QUEUES'\] .* not both"):
            dbos_settings.resolve_queues()

    def test_empty_top_level_list_raises(self, settings):
        # Django reads an empty list as "any queue", which a DBOS worker cannot process.
        settings.TASKS = {'dbos': {'BACKEND': DBOS_BACKEND, 'QUEUES': []}}
        with pytest.raises(ImproperlyConfiguredDbos, match=r"^TASKS\['dbos'\]\['QUEUES'\] is empty"):
            dbos_settings.resolve_queues()

    def test_top_level_queues_must_be_a_list(self, settings):
        settings.TASKS = {'dbos': {'BACKEND': DBOS_BACKEND, 'QUEUES': 'emails'}}
        with pytest.raises(ImproperlyConfiguredDbos, match='must be a list of queue names'):
            dbos_settings.resolve_queues()

    def test_explicit_alias_is_resolved_and_named_in_messages(self, settings):
        settings.TASKS = {
            'dbos': {'BACKEND': DBOS_BACKEND},
            'other': {'BACKEND': DBOS_BACKEND, 'OPTIONS': {'QUEUES': {'emails': {'worker_concurrency': 1}}}},
            'broken': {'BACKEND': DBOS_BACKEND, 'OPTIONS': {'QUEUES': {'emails': {'nope': 1}}}},
        }
        assert dbos_settings.resolve_queues('other') == (
            dbos_settings.QueueConfig(name='emails', worker_concurrency=1),
        )
        with pytest.raises(ImproperlyConfiguredDbos, match=r"^TASKS\['broken'\]\['OPTIONS'\]\['QUEUES'\]\['emails'\]"):
            dbos_settings.resolve_queues('broken')

    def test_names_colliding_once_stringified_raise(self, settings):
        set_dbos_options(settings, {'QUEUES': {1: None, '1': None}})
        with pytest.raises(ImproperlyConfiguredDbos, match='Duplicate queue names'):
            dbos_settings.resolve_queues()

    @pytest.mark.parametrize(
        'options',
        [
            {'partition_concurrency': 2},
            {'partition_worker_concurrency': 1},
            {'partition_limiter': {'limit': 5, 'period': 60}},
        ],
        ids=['concurrency', 'worker-concurrency', 'limiter'],
    )
    def test_a_partition_option_makes_the_queue_partitioned(self, settings, options):
        set_dbos_options(settings, {'QUEUES': {'per_user': options, 'emails': {'worker_concurrency': 1}}})
        per_user, emails = dbos_settings.resolve_queues()
        assert per_user == dbos_settings.QueueConfig(name='per_user', **options)
        assert per_user.partitioned
        assert not emails.partitioned

    def test_non_mapping_queue_capacity_raises(self, settings):
        set_dbos_options(settings, {'QUEUES': {'emails': {'capacity': 5}}})
        with pytest.raises(ImproperlyConfiguredDbos, match="\\['emails'\\]\\['capacity'\\] must be a mapping"):
            dbos_settings.resolve_queues()


class TestAppName:
    def test_defaults_to_django(self, settings):
        set_dbos_options(settings, {})
        assert dbos_settings.resolve_app_name() == 'django'

    def test_from_setting(self, settings):
        set_dbos_options(settings, {'NAME': 'myproject'})
        assert dbos_settings.resolve_app_name() == 'myproject'

    def test_resolve_settings(self, settings, monkeypatch):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        set_dbos_options(
            settings,
            {
                'NAME': 'myproject',
                'APPLICATION_VERSION': '1.2.3',
                'SYSTEM_DB_SCHEMA': 'dbos_custom',
            },
        )
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
        set_dbos_options(settings, {'QUEUES': {'emails': {'worker_concurency': 2}}})
        with pytest.raises(ImproperlyConfiguredDbos) as excinfo:
            dbos_settings.resolve_queues()
        message = str(excinfo.value)
        assert "QUEUES']['emails']" in message
        assert 'worker_concurency' in message
        assert 'worker_concurrency' in message and 'limiter' in message

    def test_non_mapping_options_are_rejected(self, settings):
        set_dbos_options(settings, {'QUEUES': {'emails': 4}})
        with pytest.raises(ImproperlyConfiguredDbos, match=r"QUEUES'\]\['emails'\] must be a mapping"):
            dbos_settings.resolve_queues()

    def test_none_means_no_options(self, settings):
        set_dbos_options(settings, {'QUEUES': {'emails': None, 'reports': {'worker_concurrency': 1}}})
        emails, reports = dbos_settings.resolve_queues()
        assert emails.worker_concurrency is None
        assert reports.worker_concurrency == 1

    def test_reported_by_the_system_check(self, settings):
        set_dbos_options(settings, {'SYSTEM_DATABASE_URL': 'postgresql://db', 'QUEUES': {'emails': {'nope': 1}}})
        errors = check_dbos_settings()
        assert [error.id for error in errors] == ['django_dbos.E001']
        assert 'nope' in errors[0].msg


class TestSerializerResolution:
    def test_unset_keeps_dbos_default(self, settings):
        set_dbos_options(settings, {})
        assert dbos_settings.resolve_serializer() is None

    @pytest.mark.parametrize(
        'value',
        [
            pytest.param('dbos.DBOSPortableJSONSerializer', id='dotted-path-to-class'),
            pytest.param('dbos.DBOSPortableJSONSerializer', id='class'),
        ],
    )
    def test_class_or_path_is_instantiated(self, settings, value):
        set_dbos_options(settings, {'SERIALIZER': DBOSPortableJSONSerializer if value == 'class' else value})
        assert isinstance(dbos_settings.resolve_serializer(), DBOSPortableJSONSerializer)

    @pytest.mark.parametrize('name', ['MsgspecJSONSerializer', 'MsgspecMsgpackSerializer'])
    def test_msgspec_serializers_resolve_from_their_path(self, settings, name):
        set_dbos_options(settings, {'SERIALIZER': f'django_dbos.serializers.{name}'})
        assert type(dbos_settings.resolve_serializer()).__name__ == name

    def test_instance_is_used_as_is(self, settings):
        instance = DBOSPortableJSONSerializer()
        set_dbos_options(settings, {'SERIALIZER': instance})
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
        set_dbos_options(settings, {'SERIALIZER': value})
        with pytest.raises(ImproperlyConfiguredDbos, match='SERIALIZER'):
            dbos_settings.resolve_serializer()
