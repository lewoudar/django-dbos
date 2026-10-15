"""Tests for the DBOS system database migration run by Django's migrate (post_migrate)."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from click.exceptions import Exit
from django.apps import apps
from django.core.management import call_command
from django.core.management.base import CommandError
from sqlalchemy.dialects import postgresql

from django_dbos import system_db
from tests.tests_helpers import set_dbos_options


@pytest.fixture
def run(mocker):
    return mocker.patch('django_dbos.system_db.run_dbos_database_migrations')


@pytest.fixture
def system_database_url(settings, monkeypatch, mocker):
    monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
    set_dbos_options(settings, {'SYSTEM_DATABASE_URL': 'postgresql://user:hunter2@db/dbos_app'})
    mocker.patch('django_dbos.system_db._is_test_database', return_value=False)
    return settings.TASKS['dbos']['OPTIONS']['SYSTEM_DATABASE_URL']


def send(**kwargs) -> None:
    kwargs = {'using': 'default', 'plan': [], 'verbosity': 1, **kwargs}
    system_db.migrate_after_django_migrate(apps.get_app_config('django_dbos'), **kwargs)


class TestMigrateAfterDjangoMigrate:
    @pytest.mark.django_db
    def test_django_migrate_runs_it_once(self, system_database_url, run):
        call_command('migrate', verbosity=0)

        run.assert_called_once()

    @pytest.mark.django_db
    def test_connected_once_when_ready_runs_again(self, system_database_url, run, mocker):
        mocker.patch('django_dbos.admin.register_admin_sites')
        apps.get_app_config('django_dbos').ready()

        call_command('migrate', verbosity=0)

        run.assert_called_once()

    @pytest.mark.django_db
    def test_flush_does_not_run_it(self, system_database_url, run):
        call_command('flush', interactive=False, verbosity=0)

        run.assert_not_called()

    def test_migrates_the_system_database(self, system_database_url, run):
        out = StringIO()

        send(stdout=out)

        run.assert_called_once_with(f'{system_database_url}?connect_timeout=10', schema='dbos', application_role=None)
        assert 'hunter2' not in out.getvalue()
        assert 'user:***@db/dbos_app' in out.getvalue()
        assert 'up to date' in out.getvalue()

    def test_verbosity_zero_is_silent(self, system_database_url, run):
        out = StringIO()

        send(stdout=out, verbosity=0)

        run.assert_called_once()
        assert out.getvalue() == ''

    @pytest.mark.parametrize(
        'kwargs',
        [
            pytest.param({'plan': None}, id='flush'),
            pytest.param({'using': 'other'}, id='other-database'),
        ],
    )
    def test_skipped(self, system_database_url, run, kwargs):
        send(stdout=StringIO(), **kwargs)

        run.assert_not_called()

    def test_disabled_by_setting(self, system_database_url, settings, run):
        set_dbos_options(settings, {**settings.TASKS['dbos']['OPTIONS'], 'MIGRATE_WITH_DJANGO': False})

        send(stdout=StringIO())

        run.assert_not_called()

    def test_skips_an_in_memory_system_database(self, system_database_url, settings, run):
        set_dbos_options(settings, {'SYSTEM_DATABASE_URL': 'sqlite:///:memory:'})

        send(stdout=StringIO())

        run.assert_not_called()

    def test_sdk_failure_names_the_opt_out(self, system_database_url, mocker):
        mocker.patch('django_dbos.system_db.run_dbos_database_migrations', side_effect=Exit(code=1))

        with pytest.raises(CommandError, match="MIGRATE_WITH_DJANGO'] = False") as excinfo:
            send(stdout=StringIO())
        assert 'DBOS migrations failed: see the error above' in str(excinfo.value)

    def test_other_failures_are_described(self, system_database_url, mocker):
        mocker.patch(
            'django_dbos.system_db.run_dbos_database_migrations',
            side_effect=RuntimeError('boom at postgresql://user:hunter2@db/dbos_app'),
        )

        with pytest.raises(CommandError, match='DBOS migrations failed: RuntimeError: boom') as excinfo:
            send(stdout=StringIO())
        assert 'hunter2' not in str(excinfo.value)

    def test_configuration_error_names_the_opt_out(self, system_database_url, settings, run):
        set_dbos_options(settings, {'QUEUES': {'x': {'nope': 1}}, 'SYSTEM_DATABASE_URL': 'postgresql://db'})

        with pytest.raises(CommandError, match=r'unknown option.*MIGRATE_WITH_DJANGO'):
            send(stdout=StringIO())
        run.assert_not_called()

    def test_unresolvable_url_names_the_opt_out(self, settings, monkeypatch, mocker, run):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        set_dbos_options(settings, {})
        mocker.patch('django_dbos.system_db._is_test_database', return_value=False)
        mocker.patch(
            'django_dbos.settings.derive_system_database_url', side_effect=system_db.ImproperlyConfiguredDbos('nope')
        )

        with pytest.raises(CommandError, match=r'nope.*MIGRATE_WITH_DJANGO'):
            send(stdout=StringIO())
        run.assert_not_called()


@pytest.mark.django_db
class TestOnTheTestDatabase:
    def test_the_test_database_is_detected(self):
        assert system_db._is_test_database('default')

    def test_django_migrate_leaves_the_system_database_alone(self, run):
        # Test database creation runs `migrate`: never derive (or reach) the system database from there.
        call_command('migrate', verbosity=0)

        run.assert_not_called()


@pytest.mark.integration
def test_creates_a_usable_system_database(settings, monkeypatch, mocker, tmp_path: Path):
    from django_dbos import client

    monkeypatch.setenv('DBOS_SYSTEM_DATABASE_URL', f'sqlite:///{tmp_path / "fresh.sqlite"}')
    mocker.patch('django_dbos.system_db._is_test_database', return_value=False)
    client.close_client()
    try:
        send(stdout=StringIO())
        assert client.get_client().list_workflows() == []
    finally:
        client.close_client()


class TestCockroachDb:
    URL = 'cockroachdb+psycopg2://root:hunter2@crdb:26257/dbos_app'

    @pytest.fixture(autouse=True)
    def cockroachdb(self, settings, monkeypatch):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        set_dbos_options(settings, {'SYSTEM_DATABASE_URL': self.URL})

    @pytest.fixture
    def engine(self, mocker):
        create_engine = mocker.patch.object(system_db.system_engine, 'create_engine')
        create_engine.return_value.dialect = postgresql.dialect()  # quotes identifiers like CockroachDB
        return create_engine

    def test_only_creates_the_database(self, run, engine):
        messages: list[str] = []

        assert system_db.migrate_system_database(echo=messages.append) is False

        run.assert_not_called()  # DBOS' migration entry point cannot reach CockroachDB
        (url,) = engine.call_args.args
        assert url == 'cockroachdb+psycopg2://root:hunter2@crdb:26257/defaultdb?connect_timeout=10'
        conn = engine.return_value.connect.return_value.execution_options.return_value.__enter__.return_value
        (statement,) = conn.execute.call_args.args
        assert str(statement) == 'CREATE DATABASE IF NOT EXISTS dbos_app'
        engine.return_value.dispose.assert_called_once_with()
        assert 'hunter2' not in ''.join(messages)
        assert messages[-1] == 'The dbos worker creates the DBOS tables in it when it launches.'

    def test_django_migrate_does_not_claim_it_is_up_to_date(self, run, engine, mocker):
        mocker.patch('django_dbos.system_db._is_test_database', return_value=False)
        out = StringIO()

        send(stdout=out)

        assert 'The dbos worker creates the DBOS tables' in out.getvalue()
        assert 'up to date' not in out.getvalue()

    def test_app_role_is_refused(self, run, engine):
        with pytest.raises(CommandError, match='--app-role is not supported'):
            system_db.migrate_system_database(echo=print, app_role='app')
        engine.assert_not_called()

    def test_a_missing_extra_is_reported(self, run, mocker):
        error = system_db.ImproperlyConfiguredDbos('needs the django-dbos[cockroachdb] extra')
        mocker.patch.object(system_db.system_engine, 'create_engine', side_effect=error)
        with pytest.raises(CommandError, match=r'django-dbos\[cockroachdb\] extra'):
            system_db.migrate_system_database(echo=lambda message: None)

    def test_a_failure_is_described_without_the_password(self, run, engine):
        connect = engine.return_value.connect
        connect.side_effect = RuntimeError(f'cannot reach {self.URL}')

        with pytest.raises(CommandError, match=r'^Creating the DBOS system database failed: RuntimeError') as exc_info:
            system_db.migrate_system_database(echo=lambda message: None)

        assert 'hunter2' not in str(exc_info.value)
        engine.return_value.dispose.assert_called_once_with()


class TestConnectTimeout:
    @pytest.mark.parametrize(
        ('url', 'expected'),
        [
            pytest.param(
                'postgresql+psycopg://user:p%40ss@db:5432/dbos_app',
                'postgresql+psycopg://user:p%40ss@db:5432/dbos_app?connect_timeout=10',
                id='added',
            ),
            pytest.param(
                'postgresql://db/dbos_app?sslmode=require',
                'postgresql://db/dbos_app?connect_timeout=10&sslmode=require',
                id='kept-query',
            ),
            pytest.param(
                'postgresql://db/dbos_app?connect_timeout=3', 'postgresql://db/dbos_app?connect_timeout=3', id='own'
            ),
            pytest.param(
                'cockroachdb+psycopg2://root@crdb:26257/dbos_app',
                'cockroachdb+psycopg2://root@crdb:26257/dbos_app?connect_timeout=10',
                id='cockroachdb',
            ),
            pytest.param('sqlite:///dbos.sqlite', 'sqlite:///dbos.sqlite', id='sqlite'),
        ],
    )
    def test_with_connect_timeout(self, url, expected):
        assert system_db._with_connect_timeout(url) == expected
