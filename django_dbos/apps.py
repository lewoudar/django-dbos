"""Django app configuration for django-dbos."""

from __future__ import annotations

from django.apps import AppConfig


class DjangoDbosConfig(AppConfig):
    name = 'django_dbos'
    verbose_name = 'Django DBOS'
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self) -> None:
        from . import admin, checks  # noqa: F401 - registers the system checks

        admin.register_admin_sites()
        # Nothing DBOS-related happens here on purpose: a web process talks to
        # the system database through a DBOSClient created on first use (see
        # client.py); only ``dbos_worker`` launches DBOS itself.
