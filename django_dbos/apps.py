"""Django app configuration for django-dbos."""

from __future__ import annotations

from django.apps import AppConfig
from django.db.models.signals import post_migrate


class DjangoDbosConfig(AppConfig):
    name = 'django_dbos'
    verbose_name = 'Django DBOS'
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self) -> None:
        from . import (
            admin,
            checks,  # noqa: F401 - registers the system checks
            system_db,
        )

        admin.register_admin_sites()
        # `manage.py migrate` also migrates the DBOS system database (a plain
        # database handle, DBOS is not launched). `sender=self`: Django sends
        # post_migrate once per installed app, run it for ours only.
        post_migrate.connect(system_db.migrate_after_django_migrate, sender=self)
        # Nothing else DBOS-related happens here on purpose: a web process talks
        # to the system database through a DBOSClient created on first use (see
        # client.py); only ``dbos worker`` launches DBOS itself.
