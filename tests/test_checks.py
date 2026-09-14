"""Tests for the django-dbos system checks."""

import pytest

from django_dbos import checks
from django_dbos.checks import check_dbos_settings, check_system_database_engine


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
