"""Schema migrations of the DBOS system database.

Shared by ``manage.py dbos migrate``. Migrating opens a plain database handle
(no worker, no background thread): it never launches DBOS.
"""

from __future__ import annotations

from collections.abc import Callable

from django.core.management.base import CommandError

from . import settings as settings_module
from .exceptions import ImproperlyConfiguredDbos, describe_error, scrub_credentials


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
        run_dbos_database_migrations(resolved.system_database_url, schema=schema, application_role=app_role)
    except Exception as exc:  # noqa: BLE001 - the SDK reports its own failure as a click Exit; keep the chain out of the output
        raise CommandError(f'DBOS migrations failed: {describe_error(exc)}') from None
