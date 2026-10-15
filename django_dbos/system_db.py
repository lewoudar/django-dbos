"""Schema migrations of the DBOS system database.

Run by ``manage.py dbos migrate`` and, unless ``MIGRATE_WITH_DJANGO`` is
``False``, at the end of Django's own ``manage.py migrate`` (``post_migrate``).
Migrating opens a plain database handle (no worker, no background thread): it
never launches DBOS.

On CockroachDB, only the database is created here: DBOS' migration entry point
builds its own PostgreSQL engine, which cannot reach CockroachDB, whereas
``DBOS.launch()`` accepts ours (``system_engine``). The worker creates DBOS'
tables when it launches (``RUN_MIGRATIONS``, see ``django_dbos.E005``).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

import sqlalchemy as sa
from dbos.cli.migration import run_dbos_database_migrations
from django.core.management.base import CommandError
from django.db import DEFAULT_DB_ALIAS, connections

from . import settings as settings_module
from . import system_engine
from .exceptions import ImproperlyConfiguredDbos, describe_error, scrub_credentials


def _opt_out_hint() -> str:
    """Appended to the errors raised from ``manage.py migrate``."""
    return (
        f'(set {settings_module.options_path("MIGRATE_WITH_DJANGO")} = False to migrate it '
        'separately with `manage.py dbos migrate`)'
    )


# Seconds before a connection attempt to a PostgreSQL system database gives up.
CONNECT_TIMEOUT_SEC = 10


def _with_connect_timeout(url: str) -> str:
    """Add libpq's ``connect_timeout`` to a PostgreSQL URL that sets none.

    The SDK connects without one: an unreachable server would block the
    migration (hence ``manage.py migrate``) indefinitely instead of failing.
    """
    parsed = sa.make_url(url)
    if not parsed.drivername.startswith(('postgresql', 'cockroachdb')) or 'connect_timeout' in parsed.query:
        return url
    return parsed.update_query_dict({'connect_timeout': str(CONNECT_TIMEOUT_SEC)}).render_as_string(hide_password=False)


def migrate_system_database(*, echo: Callable[[str], object], app_role: str | None = None) -> bool:
    """Create the DBOS system database if needed and apply DBOS' schema migrations.

    ``app_role`` is granted the permissions on the DBOS schema. Progress is
    reported through ``echo``, with credentials masked; failures raise a
    ``CommandError`` without the DSN. Returns whether DBOS' migrations were
    applied: not on CockroachDB, where the worker applies them.
    """
    try:
        resolved = settings_module.resolve_settings()
    except ImproperlyConfiguredDbos as exc:
        raise CommandError(str(exc)) from None

    if settings_module.is_cockroachdb_url(resolved.system_database_url):
        _create_cockroachdb_database(resolved.system_database_url, echo=echo, app_role=app_role)
        return False

    schema = resolved.dbos_system_schema or 'dbos'
    echo(f'Migrating the DBOS system database {scrub_credentials(resolved.system_database_url)} (schema {schema!r})')
    try:
        run_dbos_database_migrations(
            _with_connect_timeout(resolved.system_database_url), schema=schema, application_role=app_role
        )
    except Exception as exc:  # noqa: BLE001 - the SDK reports its own failure as a click Exit; keep the chain out of the output
        # The SDK prints the actual error, then raises an Exit without a message.
        detail = describe_error(exc) if str(exc) else 'see the error above'
        raise CommandError(f'DBOS migrations failed: {detail}') from None
    return True


def _create_cockroachdb_database(url: str, *, echo: Callable[[str], object], app_role: str | None) -> None:
    """Create the CockroachDB system database if needed; the worker creates DBOS' tables in it."""
    if app_role:
        raise CommandError('--app-role is not supported with a CockroachDB system database.')
    parsed = sa.make_url(_with_connect_timeout(url))
    echo(f'Creating the DBOS system database {scrub_credentials(url)} if needed (CockroachDB)')

    try:
        # Any existing database will do to run CREATE DATABASE; defaultdb always exists.
        engine = system_engine.create_engine(parsed.set(database='defaultdb').render_as_string(hide_password=False))
    except ImproperlyConfiguredDbos as exc:
        raise CommandError(str(exc)) from None

    try:
        with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
            name = engine.dialect.identifier_preparer.quote(str(parsed.database))
            conn.execute(sa.text(f'CREATE DATABASE IF NOT EXISTS {name}'))
    except Exception as exc:  # noqa: BLE001 - deliberately unchained: the cause would print the DSN
        raise CommandError(f'Creating the DBOS system database failed: {describe_error(exc)}') from None
    finally:
        engine.dispose()

    echo('The dbos worker creates the DBOS tables in it when it launches.')


def _is_test_database(alias: str) -> bool:
    """Whether the connection points to the test database (Django test runner, pytest-django).

    Test database creation runs ``migrate``: the system database URL would then
    be derived from the test database, or be the real one when set explicitly.
    ``_get_test_db_name`` is private Django API, but the only way to tell.
    """
    connection = connections[alias]
    return connection.settings_dict['NAME'] == connection.creation._get_test_db_name()


def migrate_after_django_migrate(
    sender: Any,
    *,
    using: str = DEFAULT_DB_ALIAS,
    plan: Any = None,
    verbosity: int = 1,
    stdout: Any = None,
    **kwargs: Any,
) -> None:
    """``post_migrate`` receiver: migrate the DBOS system database after ``manage.py migrate``.

    Connected for the django_dbos app only, so it runs once per ``migrate``.
    Skipped when ``post_migrate`` comes from ``flush`` (tests included), which
    sends no ``plan``; for a database other than the default one, from which
    the system database is derived; and on test databases.
    """
    try:
        if not settings_module.get_bool_option('MIGRATE_WITH_DJANGO'):
            return
    except ImproperlyConfiguredDbos as exc:
        raise CommandError(str(exc)) from None
    if plan is None or using != DEFAULT_DB_ALIAS or _is_test_database(using):
        return
    try:
        if settings_module.resolve_system_database_url() == 'sqlite:///:memory:':
            return  # a fresh in-memory database per connection: nothing to keep up to date
    except ImproperlyConfiguredDbos as exc:
        raise CommandError(f'{exc} {_opt_out_hint()}') from None

    out = stdout or sys.stdout

    def echo(message: str) -> None:
        if verbosity >= 1:
            out.write(f'{message}\n')

    try:
        migrated = migrate_system_database(echo=echo)
    except CommandError as exc:
        raise CommandError(f'{exc} {_opt_out_hint()}') from None
    if migrated:
        echo('DBOS system database is up to date.')
