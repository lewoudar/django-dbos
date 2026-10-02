"""Schema migrations of the DBOS system database.

Run by ``manage.py dbos migrate`` and, unless ``MIGRATE_WITH_DJANGO`` is
``False``, at the end of Django's own ``manage.py migrate`` (``post_migrate``).
Migrating opens a plain database handle (no worker, no background thread): it
never launches DBOS.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from django.core.management.base import CommandError
from django.db import DEFAULT_DB_ALIAS, connections

from . import settings as settings_module
from .exceptions import ImproperlyConfiguredDbos, describe_error, scrub_credentials

#: Appended to the errors raised from ``manage.py migrate``.
_OPT_OUT_HINT = (
    f"(set {settings_module.SETTING_NAME}['MIGRATE_WITH_DJANGO'] = False to migrate it "
    'separately with `manage.py dbos migrate`)'
)

#: Seconds before a connection attempt to a PostgreSQL system database gives up.
CONNECT_TIMEOUT_SEC = 10


def _with_connect_timeout(url: str) -> str:
    """Add libpq's ``connect_timeout`` to a PostgreSQL URL that sets none.

    The SDK connects without one: an unreachable server would block the
    migration (hence ``manage.py migrate``) indefinitely instead of failing.
    """
    import sqlalchemy as sa

    parsed = sa.make_url(url)
    if not parsed.drivername.startswith(('postgresql', 'cockroachdb')) or 'connect_timeout' in parsed.query:
        return url
    return parsed.update_query_dict({'connect_timeout': str(CONNECT_TIMEOUT_SEC)}).render_as_string(hide_password=False)


def migrate_system_database(*, echo: Callable[[str], object], app_role: str | None = None) -> None:
    """Create the DBOS system database if needed and apply DBOS' schema migrations.

    ``app_role`` is granted the permissions on the DBOS schema. Progress is
    reported through ``echo``, with credentials masked; failures raise a
    ``CommandError`` without the DSN.
    """
    from dbos.cli.migration import run_dbos_database_migrations

    try:
        resolved = settings_module.resolve_settings()
    except ImproperlyConfiguredDbos as exc:
        raise CommandError(str(exc)) from None

    schema = resolved.dbos_system_schema or 'dbos'
    echo(f'Migrating the DBOS system database {scrub_credentials(resolved.system_database_url)} (schema {schema!r})')
    try:
        run_dbos_database_migrations(
            _with_connect_timeout(resolved.system_database_url), schema=schema, application_role=app_role
        )
    except Exception as exc:  # noqa: BLE001 - the SDK reports its own failure as a click Exit; keep the chain out of the output
        raise CommandError(f'DBOS migrations failed: {describe_error(exc)}') from None


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
    if not settings_module.resolve_migrate_with_django():
        return
    if plan is None or using != DEFAULT_DB_ALIAS or _is_test_database(using):
        return
    try:
        if settings_module.resolve_system_database_url() == 'sqlite:///:memory:':
            return  # a fresh in-memory database per connection: nothing to keep up to date
    except ImproperlyConfiguredDbos as exc:
        raise CommandError(f'{exc} {_OPT_OUT_HINT}') from None

    out = stdout or sys.stdout

    def echo(message: str) -> None:
        if verbosity >= 1:
            out.write(f'{message}\n')

    try:
        migrate_system_database(echo=echo)
    except CommandError as exc:
        raise CommandError(f'{exc} {_OPT_OUT_HINT}') from None
    echo('DBOS system database is up to date.')
