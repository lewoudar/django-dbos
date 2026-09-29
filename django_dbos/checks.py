"""System checks for django-dbos."""

from __future__ import annotations

import sys
from typing import Any

from django.conf import settings
from django.core.checks import CheckMessage, Error, Info, Warning, register

from . import discovery
from .exceptions import ImproperlyConfiguredDbos, describe_error
from .tasks import DBOSTask, known_tasks


@register()
def check_dbos_settings(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    from . import settings as settings_module

    errors: list[CheckMessage] = []
    try:
        settings_module.resolve_settings()
    except ImproperlyConfiguredDbos as exc:
        errors.append(
            Error(
                str(exc),
                id='django_dbos.E001',
                hint=f'Fix your {settings_module.SETTING_NAME} setting or '
                'DBOS_SYSTEM_DATABASE_URL environment variable.',
            )
        )
    return errors


def _default_database_engine() -> str:
    return (settings.DATABASES.get('default', {}) or {}).get('ENGINE', '')


@register()
def check_system_database_engine(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    from . import settings as settings_module

    explicit_url = settings_module.get_dbos_setting('SYSTEM_DATABASE_URL')
    default_engine = _default_database_engine()
    if explicit_url or 'postgres' in default_engine or 'sqlite' in default_engine:
        return []

    return [
        Info(
            'The default Django database engine does not allow django-dbos to derive a DBOS system database URL.',
            id='django_dbos.I001',
            hint=f"Set {settings_module.SETTING_NAME}['SYSTEM_DATABASE_URL'] or DBOS_SYSTEM_DATABASE_URL.",
        )
    ]


def _known_tasks() -> tuple[DBOSTask, ...]:
    return known_tasks()


def _is_discovered(task: DBOSTask, discovered: list[str], exposed: set[str]) -> bool:
    """Whether the worker imports ``task``: it lives in, or is exposed by, a discovered module."""
    module = task.func.__module__
    in_discovered_module = any(module == name or module.startswith(f'{name}.') for name in discovered)
    return in_discovered_module or task.module_path in exposed


@register()
def check_task_modules(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    """Import the task modules as the worker does, then look for tasks it would not see.

    Only the tasks this process has imported so far can be inspected: the
    check is best effort.
    """
    try:
        discovered = discovery.autodiscover_tasks()
    except Exception as exc:  # noqa: BLE001 - whatever breaks the import is reported, not raised
        return [
            Error(
                f'A task module cannot be imported: {describe_error(exc)}',
                id='django_dbos.E004',
                hint='The dbos worker imports the tasks and dbos_tasks modules of every installed app '
                'when it starts: it will not start until this import is fixed.',
            )
        ]

    exposed = {
        value.module_path
        for name in discovered
        for value in vars(sys.modules[name]).values()
        if isinstance(value, DBOSTask)
    }
    return [
        Warning(
            f"Task '{task.module_path}' is not defined in a module the dbos worker imports: "
            'the worker cannot execute it and its executions stay READY.',
            id='django_dbos.W001',
            hint='Define the task in the tasks.py or dbos_tasks.py module of an installed app, or import it there.',
        )
        for task in _known_tasks()
        if not _is_discovered(task, discovered, exposed)
    ]
