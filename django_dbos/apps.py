"""Django app configuration for django-dbos."""

from __future__ import annotations

import os
import sys

from django.apps import AppConfig

from . import settings as settings_module


def _under_runserver_reloader_parent() -> bool:
    """True when this process is the autoreloader parent of ``runserver``.

    Both the parent and the child import the app config; only the child
    actually serves requests (``RUN_MAIN``), so only it should initialize DBOS.
    """
    if 'runserver' not in sys.argv:
        return False
    return os.environ.get('RUN_MAIN') != 'true'


class DjangoDbosConfig(AppConfig):
    name = 'django_dbos'
    verbose_name = 'Django DBOS'
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self) -> None:
        from . import admin, dbos_setup

        admin.register_admin_sites()

        if not settings_module.get_dbos_setting('AUTO_INIT', True):
            return
        if _under_runserver_reloader_parent():
            return
        # Initialize without launching background services: this process may
        # only enqueue tasks. Workers must be started with dbos_worker.
        dbos_setup.ensure_initialized()
