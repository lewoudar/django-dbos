"""System checks for django-dbos."""

from __future__ import annotations

import contextlib
import difflib
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


@register()
def check_unknown_settings(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    """Report ``DJANGO_DBOS`` keys django-dbos does not read: a typo would be silently ignored."""
    from . import settings as settings_module

    user_settings = getattr(settings, settings_module.SETTING_NAME, None)
    if not isinstance(user_settings, dict):
        return []
    errors: list[CheckMessage] = []
    for key in user_settings:
        if key in settings_module.KNOWN_SETTINGS:
            continue
        matches = difflib.get_close_matches(str(key), settings_module.KNOWN_SETTINGS, n=1)
        hint = (
            f"Did you mean '{matches[0]}'?" if matches else f'Valid keys: {", ".join(settings_module.KNOWN_SETTINGS)}.'
        )
        errors.append(Error(f'{settings_module.SETTING_NAME}: unknown key {key!r}.', id='django_dbos.E003', hint=hint))
    return errors


def _dbos_backend_queues() -> dict[str, set[str]]:
    """The queues each ``TASKS`` alias using the DBOS backend accepts (an empty set accepts any)."""
    from django.tasks.base import DEFAULT_TASK_QUEUE_NAME
    from django.utils.module_loading import import_string

    from .backends import DBOSBackend

    backends: dict[str, set[str]] = {}
    for alias, params in (getattr(settings, 'TASKS', None) or {}).items():
        try:
            backend_class = import_string(params['BACKEND'])
        except (ImportError, KeyError, TypeError):
            continue  # an invalid backend is Django's own checks' business
        if isinstance(backend_class, type) and issubclass(backend_class, DBOSBackend):
            backends[alias] = set(params.get('QUEUES', [DEFAULT_TASK_QUEUE_NAME]))
    return backends


def _known_tasks() -> tuple[DBOSTask, ...]:
    return known_tasks()


@register()
def check_declared_queues(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    """Every queue a DBOS task can be enqueued on must be declared in ``DJANGO_DBOS["QUEUES"]``.

    A backend listing its queues is checked through that list; a backend
    accepting any queue (``QUEUES: []``) through the tasks known so far.
    """
    from . import settings as settings_module

    try:
        declared = {queue.name for queue in settings_module.resolve_queues()}
    except ImproperlyConfiguredDbos:
        return []  # reported by django_dbos.E001

    def undeclared(queue: str, owner: str) -> Error:
        return Error(
            f"Queue {queue!r} of {owner} is not declared in {settings_module.SETTING_NAME}['QUEUES']: "
            'enqueuing a task on it raises QueueNotDeclared.',
            id='django_dbos.E002',
            hint=f"Add it to {settings_module.SETTING_NAME}['QUEUES'], for example {{{queue!r}: None}}.",
        )

    backends = _dbos_backend_queues()
    errors: list[CheckMessage] = [
        undeclared(queue, f'TASKS[{alias!r}]')
        for alias, queues in backends.items()
        for queue in sorted(queues - declared)
    ]
    open_aliases = {alias for alias, queues in backends.items() if not queues}
    if open_aliases:
        with contextlib.suppress(Exception):  # a failing import is reported by django_dbos.E004
            discovery.autodiscover_tasks()
        errors.extend(
            undeclared(task.queue_name, f"task '{task.module_path}'")
            for task in _known_tasks()
            if task.backend in open_aliases and task.queue_name not in declared
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
        for task in _known_tasks()
        if not _is_discovered(task, discovered, exposed)
    ]
