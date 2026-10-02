"""Django app configuration for django-dbos."""

from __future__ import annotations

from django.apps import AppConfig


class DjangoDbosConfig(AppConfig):
    name = 'django_dbos'
    verbose_name = 'Django DBOS'
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self) -> None:
        from django.db.models.signals import post_migrate

        from . import admin, checks, system_db  # noqa: F401 - registers the system checks

        admin.register_admin_sites()
        # `manage.py migrate` also migrates the DBOS system database (a plain
        # database handle, DBOS is not launched).
        post_migrate.connect(
            system_db.migrate_after_django_migrate, sender=self, dispatch_uid='django_dbos.system_db.migrate'
        )
        # Nothing else DBOS-related happens here on purpose: a web process talks
        # to the system database through a DBOSClient created on first use (see
        # client.py); only ``dbos worker`` launches DBOS itself.
