"""System checks for django-dbos."""

from __future__ import annotations

import difflib
import sys
from typing import Any

from django.core.checks import CheckMessage, Error, Tags, Warning, register

from . import discovery
from . import settings as settings_module
from .exceptions import ImproperlyConfiguredDbos, describe_error
from .tasks import DBOSTask, known_tasks


@register()
def check_dbos_settings(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    errors: list[CheckMessage] = []
    try:
        settings_module.resolve_settings()
    except ImproperlyConfiguredDbos as exc:
        errors.append(
            Error(
                str(exc),
                id='django_dbos.E001',
                hint=f'Fix {settings_module.backend_path()} or the DBOS_SYSTEM_DATABASE_URL environment variable.',
            )
        )
    return errors


@register()
def check_single_dbos_backend(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    """DBOS is a per-process singleton: one system database, one configuration, so one backend."""
    aliases = settings_module.dbos_backend_aliases()
    if len(aliases) <= 1:
        return []
    return [
        Error(
            f'Several TASKS entries use the DBOS backend ({", ".join(map(repr, aliases))}): '
            f'django-dbos supports a single one and only reads TASKS[{aliases[0]!r}].',
            id='django_dbos.E002',
            hint='Keep one DBOS backend and declare all its queues in it.',
        )
    ]


@register()
def check_unknown_settings(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    """Report ``OPTIONS`` keys django-dbos does not read: a typo would be silently ignored."""
    errors: list[CheckMessage] = []
    for key in settings_module.get_dbos_options():
        if key in settings_module.KNOWN_SETTINGS:
            continue
        matches = difflib.get_close_matches(str(key), settings_module.KNOWN_SETTINGS, n=1)
        hint = (
            f"Did you mean '{matches[0]}'?" if matches else f'Valid keys: {", ".join(settings_module.KNOWN_SETTINGS)}.'
        )
        errors.append(
            Error(f'{settings_module.options_path()}: unknown key {key!r}.', id='django_dbos.E003', hint=hint)
        )
    return errors


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
        for task in known_tasks()
        if not _is_discovered(task, discovered, exposed)
    ]


@register()
def check_in_memory_system_database(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    """An in-memory SQLite system database lives in one process: web and worker never share it."""
    try:
        url = settings_module.resolve_system_database_url()
    except ImproperlyConfiguredDbos:
        return []  # reported by django_dbos.E001
    if not (url.startswith('sqlite') and ':memory:' in url):
        return []
    return [
        Warning(
            'The DBOS system database is an in-memory SQLite database: each process gets its own, '
            'so tasks enqueued by the web process never reach the worker.',
            id='django_dbos.W002',
            hint=f'Use a file-based Django database, or set {settings_module.options_path("SYSTEM_DATABASE_URL")}.',
        )
    ]


@register(Tags.security, deploy=True)
def check_serializer(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    if settings_module.get_dbos_setting('SERIALIZER') is not None:
        return []
    return [
        Warning(
            'No SERIALIZER is set: DBOS stores task arguments, results and errors with pickle, so anyone '
            'able to write to the system database can run code in your workers and web processes.',
            id='django_dbos.W003',
            hint=f'If your task arguments and results are plain data, set {settings_module.options_path("SERIALIZER")} '
            "to 'django_dbos.serializers.MsgspecJSONSerializer' (extra django-dbos[msgspec]) "
            "or 'dbos.DBOSPortableJSONSerializer'.",
        )
    ]


@register(deploy=True)
def check_executor_id(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    if settings_module.resolve_executor_id() is not None:
        return []
    return [
        Warning(
            'No executor ID is set: every worker is named "local", so workflow records, '
            '`dbos status` and recovery on restart cannot tell the workers apart.',
            id='django_dbos.W004',
            hint=f'Give each worker a stable identity with {settings_module.options_path("EXECUTOR_ID")} '
            'or the DBOS__VMID environment variable (the machine or pod name).',
        )
    ]


@register()
def check_dbos_backend_configured(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    """django_dbos is installed but no backend uses it: tasks never reach DBOS."""
    if settings_module.dbos_backend_aliases():
        return []
    return [
        Warning(
            'django_dbos is installed but no TASKS entry uses its backend: tasks are not run by DBOS '
            'and the django-dbos settings are not read.',
            id='django_dbos.W005',
            hint="Add a TASKS entry with 'BACKEND': 'django_dbos.backends.DBOSBackend'.",
        )
    ]
