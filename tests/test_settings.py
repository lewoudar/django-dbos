"""Tests for the DJANGO_DBOS setting resolution."""

import pytest

from django_dbos import settings as dbos_settings
from django_dbos.exceptions import ImproperlyConfiguredDbos


class TestSystemDatabaseUrlDerivation:
    def test_sqlite_database(self):
        url = dbos_settings.derive_system_database_url(
            {"ENGINE": "django.db.backends.sqlite3", "NAME": "myproject.sqlite3"}
        )
        assert url == "sqlite:///myproject.sqlite3-dbos.sqlite"

    def test_sqlite_memory_database(self):
        url = dbos_settings.derive_system_database_url({"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"})
        assert url == "sqlite:///:memory:"

    def test_postgres_database(self):
        url = dbos_settings.derive_system_database_url(
            {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "myproject",
                "USER": "kevin",
                "PASSWORD": "hunter2",
                "HOST": "db.example.com",
                "PORT": "5433",
            }
        )
        assert url == "postgresql+psycopg://kevin:hunter2@db.example.com:5433/dbos_myproject"

    def test_postgres_credentials_are_percent_encoded(self):
        url = dbos_settings.derive_system_database_url(
            {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "myproject",
                "USER": "user@domain",
                "PASSWORD": "p@ss:word/",
            }
        )
        assert url == (
            "postgresql+psycopg://user%40domain:p%40ss%3Aword%2F@localhost:5432/dbos_myproject"
        )

    def test_postgres_defaults(self):
        url = dbos_settings.derive_system_database_url({"ENGINE": "django.db.backends.postgresql", "NAME": "myproject"})
        assert url == "postgresql+psycopg://:@localhost:5432/dbos_myproject"

    def test_postgres_custom_prefix(self, settings):
        settings.DJANGO_DBOS = {"SYSTEM_DB_PREFIX": "mydbos_"}
        url = dbos_settings.derive_system_database_url({"ENGINE": "django.db.backends.postgresql", "NAME": "myproject"})
        assert url == "postgresql+psycopg://:@localhost:5432/mydbos_myproject"

    def test_unsupported_engine_raises(self):
        with pytest.raises(ImproperlyConfiguredDbos):
            dbos_settings.derive_system_database_url({"ENGINE": "django.db.backends.oracle", "NAME": "orcl"})


class TestSystemDatabaseUrlResolution:
    def test_env_var_wins(self, settings, monkeypatch):
        monkeypatch.setenv("DBOS_SYSTEM_DATABASE_URL", "postgresql://env-url")
        assert dbos_settings.resolve_system_database_url() == "postgresql://env-url"

    def test_setting_wins(self, settings, monkeypatch):
        monkeypatch.setenv("DBOS_SYSTEM_DATABASE_URL", "postgresql://env-url")
        settings.DJANGO_DBOS = {"SYSTEM_DATABASE_URL": "postgresql://setting-url"}
        assert dbos_settings.resolve_system_database_url() == "postgresql://setting-url"

    def test_falls_back_to_derivation(self, settings, monkeypatch):
        monkeypatch.delenv("DBOS_SYSTEM_DATABASE_URL", raising=False)
        settings.DJANGO_DBOS = {"SYSTEM_DATABASE_URL": None}
        # The derived URL follows the configured Django database (pytest-django
        # may rewrite the test database name, so assert on the derivation rule).
        url = dbos_settings.resolve_system_database_url()
        assert url.startswith("sqlite:///")
        assert url.endswith("-dbos.sqlite") or url == "sqlite:///:memory:"


class TestQueues:
    def test_default_queue(self, settings):
        settings.DJANGO_DBOS = {}
        queues = dbos_settings.resolve_queues()
        assert [q.name for q in queues] == ["default"]

    def test_declared_queues(self, settings):
        settings.DJANGO_DBOS = {
            "QUEUES": {
                "emails": {"worker_concurrency": 4},
                "reports": {"global_concurrency": 2},
            },
        }
        queues = dbos_settings.resolve_queues()
        assert [q.name for q in queues] == ["emails", "reports"]
        assert queues[0].worker_concurrency == 4
        assert queues[1].global_concurrency == 2

    def test_non_mapping_queues_raise(self, settings):
        settings.DJANGO_DBOS = {"QUEUES": ["emails"]}
        with pytest.raises(ImproperlyConfiguredDbos):
            dbos_settings.resolve_queues()


class TestAppName:
    def test_defaults_to_django(self, settings):
        settings.DJANGO_DBOS = {}
        assert dbos_settings.resolve_app_name() == "django"

    def test_from_setting(self, settings):
        settings.DJANGO_DBOS = {"NAME": "myproject"}
        assert dbos_settings.resolve_app_name() == "myproject"

    def test_resolve_settings(self, settings, monkeypatch):
        monkeypatch.delenv("DBOS_SYSTEM_DATABASE_URL", raising=False)
        settings.DJANGO_DBOS = {
            "NAME": "myproject",
            "APPLICATION_VERSION": "1.2.3",
            "SYSTEM_DB_SCHEMA": "dbos_custom",
        }
        resolved = dbos_settings.resolve_settings()
        assert resolved.app_name == "myproject"
        assert resolved.application_version == "1.2.3"
        assert resolved.dbos_system_schema == "dbos_custom"
        assert [q.name for q in resolved.queues] == ["default"]


class TestCredentialScrubbing:
    def test_postgres_dsn_password_masked(self):
        from django_dbos.dbos_setup import _scrub_credentials

        message = (
            "connection to server failed: "
            "postgresql+psycopg://app_user:s3cret-pw@db.internal:5432/dbos_mydb"
        )
        scrubbed = _scrub_credentials(message)
        assert "s3cret-pw" not in scrubbed
        assert "postgresql+psycopg://app_user:***@db.internal:5432/dbos_mydb" in scrubbed

    def test_schemes_and_special_characters(self):
        from django_dbos.dbos_setup import _scrub_credentials

        scrubbed = _scrub_credentials("mysql://u:p@h/db then postgresql://u2:p2%40x@h2/db2")
        assert "mysql://u:***@h/db" in scrubbed
        assert "postgresql://u2:***@h2/db2" in scrubbed

    def test_message_without_dsn_unchanged(self):
        from django_dbos.dbos_setup import _scrub_credentials

        message = "No credentials in here, just text."
        assert _scrub_credentials(message) == message
