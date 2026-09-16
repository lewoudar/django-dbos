"""Tests for the process-wide DBOSClient (non-worker processes)."""

import pytest
from dbos import DBOSPortableJSONSerializer

from django_dbos import client
from django_dbos.exceptions import ImproperlyConfiguredDbos


@pytest.fixture(autouse=True)
def _fresh_client():
    client.close_client()
    yield
    client.close_client()


class TestGetClient:
    def test_client_is_created_once_from_settings(self, settings, monkeypatch, mocker):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        settings.DJANGO_DBOS = {
            'NAME': 'myproject',
            'SYSTEM_DATABASE_URL': 'postgresql://user:secret@db/dbos_myproject',
            'SYSTEM_DB_SCHEMA': 'custom',
        }
        client_cls = mocker.patch('dbos.DBOSClient')

        first = client.get_client()
        second = client.get_client()

        assert first is second is client_cls.return_value
        client_cls.assert_called_once_with(
            system_database_url='postgresql://user:secret@db/dbos_myproject',
            dbos_system_schema='custom',
            application_name='myproject',
            lazy=True,
            retry_connection_errors=False,
        )

    def test_serializer_is_forwarded(self, settings, monkeypatch, mocker):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        serializer = DBOSPortableJSONSerializer()
        settings.DJANGO_DBOS = {'SYSTEM_DATABASE_URL': 'postgresql://db', 'SERIALIZER': serializer}
        client_cls = mocker.patch('dbos.DBOSClient')

        client.get_client()

        assert client_cls.call_args.kwargs['serializer'] is serializer

    def test_client_never_launches_dbos(self, settings, monkeypatch, mocker):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        settings.DJANGO_DBOS = {'SYSTEM_DATABASE_URL': 'postgresql://db'}
        mocker.patch('dbos.DBOSClient')
        launch = mocker.patch('dbos.DBOS.launch')

        client.get_client()

        launch.assert_not_called()

    def test_close_client_destroys_and_forgets(self, settings, monkeypatch, mocker):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        settings.DJANGO_DBOS = {'SYSTEM_DATABASE_URL': 'postgresql://db'}
        client_cls = mocker.patch('dbos.DBOSClient')

        instance = client.get_client()
        client.close_client()
        client.close_client()  # idempotent

        instance.destroy.assert_called_once_with()
        assert client.get_client() is client_cls.return_value
        assert client_cls.call_count == 2

    def test_configuration_errors_are_scrubbed(self, settings, monkeypatch, mocker):
        monkeypatch.delenv('DBOS_SYSTEM_DATABASE_URL', raising=False)
        settings.DJANGO_DBOS = {'SYSTEM_DATABASE_URL': 'postgresql://user:hunter2@db/dbos'}
        mocker.patch('dbos.DBOSClient', side_effect=ValueError('cannot use postgresql://user:hunter2@db/dbos'))

        with pytest.raises(ImproperlyConfiguredDbos) as excinfo:
            client.get_client()

        assert 'hunter2' not in str(excinfo.value)
        assert str(excinfo.value) == 'ValueError: cannot use postgresql://user:***@db/dbos'
        # Unchained on purpose: the cause would carry the unmasked DSN.
        assert excinfo.value.__cause__ is None
