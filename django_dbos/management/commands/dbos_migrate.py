"""Create and migrate the DBOS system database without starting a worker."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from django_dbos import settings as settings_module
from django_dbos.exceptions import ImproperlyConfiguredDbos, describe_error, scrub_credentials


class Command(BaseCommand):
    help = (
        'Create the DBOS system database and apply its migrations (from the DJANGO_DBOS '
        'setting), without launching a worker. Run it in your release step, next to migrate.'
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            '--app-role',
            default=None,
            help='Database role to grant the DBOS schema permissions to (the role your workers and web processes use).',
        )

    def handle(self, *args: Any, **options: Any) -> None:
        # The SDK's own `dbos migrate` implementation: creates the database and
        # applies the schema migrations through a plain SystemDatabase handle —
        # no launch, no background thread, no workflow claimed.
        from dbos.cli.migration import run_dbos_database_migrations

        try:
            resolved = settings_module.resolve_settings()
        except ImproperlyConfiguredDbos as exc:
            raise CommandError(str(exc)) from None

        schema = resolved.dbos_system_schema or 'dbos'
        self.stdout.write(
            f'Migrating the DBOS system database {scrub_credentials(resolved.system_database_url)} (schema {schema!r})'
        )
        try:
            run_dbos_database_migrations(
                resolved.system_database_url,
                schema=schema,
                application_role=options['app_role'],
            )
        except Exception as exc:  # noqa: BLE001 - the SDK reports its own failure as a click Exit; keep the chain out of the output
            raise CommandError(f'DBOS migrations failed: {describe_error(exc)}') from None
        self.stdout.write(self.style.SUCCESS('DBOS system database is up to date.'))
