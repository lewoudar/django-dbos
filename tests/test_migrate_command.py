"""Tests for the dbos migrate management command."""

from __future__ import annotations

import contextlib
from io import StringIO
from pathlib import Path

import pytest
from click.exceptions import Exit
from django.core.management import call_command


def migrate(*args: str) -> str:
    # django-click (2.5.0, the latest released version) does not yet support
    # `call_command(..., stdout=...)` (its `base_stealth_options` is empty, so
    # Django's own call_command rejects the kwarg) - redirect sys.stdout ourselves.
    out = StringIO()
    with contextlib.redirect_stdout(out):
        call_command('dbos', 'migrate', *args)
    return out.getvalue()


class TestDbosMigrate:
    def test_migrates_from_the_resolved_settings(self, settings, monkeypatch, mocker):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        settings.DJANGO_DBOS = {
            'SYSTEM_DATABASE_URL': 'postgresql://user:hunter2@db/dbos_app',
            'SYSTEM_DB_SCHEMA': 'custom',
        }
        run = mocker.patch('django_dbos.system_db.run_dbos_database_migrations')

        output = migrate('--app-role', 'app')

        run.assert_called_once_with(
            'postgresql://user:hunter2@db/dbos_app?connect_timeout=10', schema='custom', application_role='app'
        )
        assert 'hunter2' not in output
        assert 'user:***@db/dbos_app' in output
        assert 'up to date' in output

    def test_never_launches_dbos(self, settings, monkeypatch, mocker):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        settings.DJANGO_DBOS = {'SYSTEM_DATABASE_URL': 'postgresql://db'}
        mocker.patch('django_dbos.system_db.run_dbos_database_migrations')
        launch = mocker.patch('dbos.DBOS.launch')
        ensure_launched = mocker.patch('django_dbos.dbos_setup.ensure_launched')

        migrate()

        launch.assert_not_called()
        ensure_launched.assert_not_called()

    def test_sdk_failure_is_a_command_error(self, settings, monkeypatch, mocker):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        settings.DJANGO_DBOS = {'SYSTEM_DATABASE_URL': 'postgresql://db'}
        # The SDK reports failures as a click Exit after echoing them.
        mocker.patch('django_dbos.system_db.run_dbos_database_migrations', side_effect=Exit(code=1))

        err = StringIO()
        with contextlib.redirect_stdout(StringIO()), contextlib.redirect_stderr(err), pytest.raises(Exit) as excinfo:
            call_command('dbos', 'migrate')
        assert excinfo.value.exit_code == 1
        assert 'DBOS migrations failed' in err.getvalue()

    def test_configuration_errors_are_command_errors(self, settings, monkeypatch):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        settings.DJANGO_DBOS = {'SYSTEM_DATABASE_URL': 'postgresql://db', 'QUEUES': {'x': {'nope': 1}}}
        err = StringIO()
        with contextlib.redirect_stdout(StringIO()), contextlib.redirect_stderr(err), pytest.raises(Exit) as excinfo:
            call_command('dbos', 'migrate')
        assert excinfo.value.exit_code == 1
        assert 'unknown option' in err.getvalue()

    @pytest.mark.integration
    def test_creates_a_usable_system_database(self, settings, monkeypatch, tmp_path: Path):
        # End to end on SQLite: an empty file becomes a migrated system database
        # that a client can query.
        from django_dbos import client

        url = f'sqlite:///{tmp_path / "fresh.sqlite"}'
        monkeypatch.setenv('DBOS_SYSTEM_DATABASE_URL', url)
        client.close_client()
        try:
            migrate()
            assert client.get_client().list_workflows() == []
        finally:
            client.close_client()
