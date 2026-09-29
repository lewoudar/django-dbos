"""Configuration for django-dbos.

Everything is driven by a single ``DJANGO_DBOS`` setting, a dict of options.
Options not set there fall back to environment variables where DBOS supports
them (``DBOS_SYSTEM_DATABASE_URL``, ...). When no URL is given at all, the
system database URL is derived from the default Django database connection:

- Postgres: same server, dedicated database ``<SYSTEM_DB_PREFIX><dbname>``
  (created with DBOS' migrations by ``manage.py dbos migrate``, or on worker
  launch unless ``RUN_MIGRATIONS`` is ``False``).
- SQLite: a file next to the Django database.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

from .exceptions import ImproperlyConfiguredDbos

SETTING_NAME = 'DJANGO_DBOS'
DEFAULT_SYSTEM_DB_PREFIX = 'dbos_'

DEFAULT_QUEUES = ('default',)

#: Every key ``DJANGO_DBOS`` accepts; the system checks report any other one.
KNOWN_SETTINGS = (
    'NAME',
    'APPLICATION_VERSION',
    'SYSTEM_DATABASE_URL',
    'SYSTEM_DB_PREFIX',
    'SYSTEM_DB_SCHEMA',
    'QUEUES',
    'EXECUTOR_ID',
    'SERIALIZER',
    'CAPACITY',
    'RUN_MIGRATIONS',
    'SCHEDULER_POLLING_INTERVAL_SEC',
    'ADMIN_ENABLED',
    'ADMIN_SITE',
)


def get_dbos_setting(key: str, default: Any = None) -> Any:
    """Read a ``DJANGO_DBOS`` setting key."""
    user_settings = getattr(settings, SETTING_NAME, None) or {}
    return user_settings.get(key, default)


@dataclass(frozen=True)
class QueueConfig:
    """Configuration of a DBOS queue declared from Django settings."""

    name: str
    worker_concurrency: int | None = None
    global_concurrency: int | None = None
    limiter: dict[str, Any] | None = None
    polling_interval_sec: float | None = None
    #: Per-queue capacity thresholds (django-dbos only, never sent to DBOS),
    #: same keys as ``DJANGO_DBOS["CAPACITY"]`` minus ``ADVISOR``.
    capacity: dict[str, Any] | None = None


#: ``DJANGO_DBOS["CAPACITY"]`` threshold keys and their defaults; see ``capacity.advise_queue``.
CAPACITY_THRESHOLD_DEFAULTS: dict[str, float | int] = {
    'SCALE_UP_BACKLOG_AGE_SECONDS': 60.0,
    'SCALE_DOWN_MAX_RUNNING': 0,
}


@dataclass(frozen=True)
class CapacityConfig:
    """Resolved ``DJANGO_DBOS["CAPACITY"]``: global thresholds and optional custom advisor."""

    thresholds: dict[str, float | int] = field(default_factory=lambda: dict(CAPACITY_THRESHOLD_DEFAULTS))
    advisor: Any = None

    def thresholds_for(self, queue: QueueConfig | None) -> dict[str, float | int]:
        """Global thresholds overridden by the queue's own ``capacity`` options, as lowercase keys."""
        merged = {**self.thresholds, **((queue.capacity if queue is not None else None) or {})}
        return {key.lower(): value for key, value in merged.items()}


@dataclass(frozen=True)
class DbosSettings:
    """Resolved django-dbos configuration."""

    app_name: str
    system_database_url: str
    application_version: str | None = None
    dbos_system_schema: str | None = None
    executor_id: str | None = None
    serializer: Any = None
    queues: tuple[QueueConfig, ...] = field(default_factory=tuple)
    capacity: CapacityConfig = field(default_factory=CapacityConfig)


def _django_database_settings() -> dict[str, Any]:
    default_db = settings.DATABASES['default']
    if default_db.get('ENGINE') == 'django.db.backends.dummy' or 'NAME' not in default_db:
        raise ImproperlyConfiguredDbos(
            'No default Django database is configured. django-dbos derives the DBOS '
            'system database from it, or you can set '
            f"{SETTING_NAME}['SYSTEM_DATABASE_URL'] / DBOS_SYSTEM_DATABASE_URL."
        )
    return default_db


def _sqlite_system_database_url(django_db: dict[str, Any]) -> str:
    name = django_db['NAME']
    if name == ':memory:':
        return 'sqlite:///:memory:'
    return f'sqlite:///{name}-dbos.sqlite'


def _postgres_url_from_django(django_db: dict[str, Any], db_name: str) -> str:
    from urllib.parse import quote

    # percent-encode the credentials (a password may contain '@', ':' or '/')
    user = quote(django_db.get('USER') or '', safe='')
    password = quote(django_db.get('PASSWORD') or '', safe='')
    host = django_db.get('HOST') or 'localhost'
    port = django_db.get('PORT') or '5432'
    return f'postgresql+psycopg://{user}:{password}@{host}:{port}/{db_name}'


def _postgres_system_database_url(django_db: dict[str, Any]) -> str:
    prefix = get_dbos_setting('SYSTEM_DB_PREFIX', DEFAULT_SYSTEM_DB_PREFIX)
    return _postgres_url_from_django(django_db, f'{prefix}{django_db["NAME"]}')


def derive_system_database_url(django_db: dict[str, Any]) -> str:
    """Derive the DBOS system database URL from a Django database config dict."""
    engine = django_db.get('ENGINE') or ''
    if 'sqlite' in engine:
        return _sqlite_system_database_url(django_db)
    if 'postgres' in engine:
        return _postgres_system_database_url(django_db)
    raise ImproperlyConfiguredDbos(
        f"Unsupported database engine '{engine}' for deriving the DBOS system "
        'database. Set '
        f"{SETTING_NAME}['SYSTEM_DATABASE_URL'] or DBOS_SYSTEM_DATABASE_URL explicitly."
    )


def resolve_system_database_url() -> str:
    """Resolve the DBOS system database URL, by order of precedence:

    1. ``DJANGO_DBOS["SYSTEM_DATABASE_URL"]``
    2. ``DBOS_SYSTEM_DATABASE_URL`` environment variable
    3. Derived from the default Django database connection
    """
    url = get_dbos_setting('SYSTEM_DATABASE_URL') or os.environ.get('DBOS_SYSTEM_DATABASE_URL')
    if url:
        return url
    return derive_system_database_url(_django_database_settings())


def resolve_app_name() -> str:
    """Application name for DBOS, defaulting to the Django project name."""
    return str(get_dbos_setting('NAME') or os.environ.get('DBOS_APPLICATION_NAME') or 'django')


def resolve_executor_id() -> str | None:
    """Identity of this worker process for DBOS (``executor_id``).

    Resolved from ``DJANGO_DBOS["EXECUTOR_ID"]``, then the ``DBOS__VMID``
    environment variable DBOS itself honours; ``None`` leaves DBOS' default
    (``"local"``). Workflow rows, recovery on restart and worker heartbeats
    are all keyed by it, so give each worker instance a *stable* identity
    (e.g. ``FLY_MACHINE_ID`` on fly.io, the pod name on Kubernetes).
    """
    executor_id = get_dbos_setting('EXECUTOR_ID') or os.environ.get('DBOS__VMID')
    return str(executor_id) if executor_id else None


def resolve_serializer() -> Any:
    """The DBOS serializer for task arguments, results and errors, or ``None``.

    ``DJANGO_DBOS["SERIALIZER"]`` is a ``dbos.Serializer`` instance, a class
    (instantiated without arguments) or the dotted path of either. ``None``
    keeps DBOS' default, which is **pickle**: anyone who can write to the
    system database can then run code in the workers and in every process
    reading results. ``dbos.DBOSPortableJSONSerializer`` is the safe
    alternative when task arguments and results are plain data.

    The worker and every client must use the same serializer.
    """
    from dbos import Serializer
    from django.utils.module_loading import import_string

    value = get_dbos_setting('SERIALIZER')
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = import_string(value)
        except ImportError as exc:
            raise ImproperlyConfiguredDbos(f"{SETTING_NAME}['SERIALIZER']: {exc}") from exc
    if isinstance(value, type):
        value = value()
    if not isinstance(value, Serializer):
        raise ImproperlyConfiguredDbos(
            f"{SETTING_NAME}['SERIALIZER'] must be a dbos.Serializer instance, subclass or dotted path."
        )
    return value


_QUEUE_OPTIONS = tuple(name for name in QueueConfig.__dataclass_fields__ if name != 'name')


def _validate_capacity_thresholds(mapping: Any, where: str) -> dict[str, float | int]:
    if not isinstance(mapping, dict):
        raise ImproperlyConfiguredDbos(f'{where} must be a mapping of thresholds.')
    unknown = sorted(set(mapping) - set(CAPACITY_THRESHOLD_DEFAULTS))
    if unknown:
        raise ImproperlyConfiguredDbos(
            f'{where}: unknown threshold(s) {", ".join(unknown)}. Valid thresholds: {", ".join(CAPACITY_THRESHOLD_DEFAULTS)}.'
        )
    thresholds: dict[str, float | int] = {}
    for key, value in mapping.items():
        if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
            raise ImproperlyConfiguredDbos(f'{where}[{key!r}] must be a non-negative number, got {value!r}.')
        thresholds[key] = value
    return thresholds


def resolve_capacity() -> CapacityConfig:
    """Resolve ``DJANGO_DBOS["CAPACITY"]``: thresholds of the scaling advice and an optional advisor.

    ``ADVISOR`` is a callable ``(queue_name, metrics, thresholds) -> str`` (or
    its dotted path) replacing the default heuristic, ``capacity.advise_queue``.
    """
    from django.utils.module_loading import import_string

    where = f"{SETTING_NAME}['CAPACITY']"
    setting = get_dbos_setting('CAPACITY')
    if setting is None:
        setting = {}
    if not isinstance(setting, dict):
        raise ImproperlyConfiguredDbos(f'{where} must be a mapping.')
    advisor = setting.get('ADVISOR')
    if isinstance(advisor, str):
        try:
            advisor = import_string(advisor)
        except ImportError as exc:
            raise ImproperlyConfiguredDbos(f"{where}['ADVISOR']: {exc}") from exc
    if advisor is not None and not callable(advisor):
        raise ImproperlyConfiguredDbos(f"{where}['ADVISOR'] must be a callable or its dotted path.")
    overrides = _validate_capacity_thresholds({k: v for k, v in setting.items() if k != 'ADVISOR'}, where)
    return CapacityConfig(thresholds={**CAPACITY_THRESHOLD_DEFAULTS, **overrides}, advisor=advisor)


def _queue_config(name: str, options: Any) -> QueueConfig:
    if options is None:
        return QueueConfig(name=name)
    if not isinstance(options, dict):
        raise ImproperlyConfiguredDbos(
            f"{SETTING_NAME}['QUEUES']['{name}'] must be a mapping of options (or None), got {type(options).__name__}."
        )
    unknown = sorted(set(options) - set(_QUEUE_OPTIONS))
    if unknown:
        raise ImproperlyConfiguredDbos(
            f"{SETTING_NAME}['QUEUES']['{name}']: unknown option(s) {', '.join(unknown)}. "
            f'Valid options: {", ".join(_QUEUE_OPTIONS)}.'
        )
    if options.get('capacity') is not None:
        options = {
            **options,
            'capacity': _validate_capacity_thresholds(
                options['capacity'], f"{SETTING_NAME}['QUEUES']['{name}']['capacity']"
            ),
        }
    return QueueConfig(name=name, **options)


def resolve_queues() -> tuple[QueueConfig, ...]:
    """Resolve declared DBOS queues.

    Users declare queues in ``DJANGO_DBOS["QUEUES"]`` as a mapping of queue
    name to queue options (``None`` for none); ``("default",)`` when unset.
    Every queue a task targets (``TASKS[alias]["QUEUES"]``, ``queue_name``)
    must be declared here: the backend raises ``QueueNotDeclared`` otherwise.
    """
    queues_setting = get_dbos_setting('QUEUES') or {}
    if not isinstance(queues_setting, dict):
        raise ImproperlyConfiguredDbos(f"{SETTING_NAME}['QUEUES'] must be a mapping of queue name to options.")
    queues = tuple(_queue_config(str(name), options) for name, options in queues_setting.items()) or tuple(
        QueueConfig(name=name) for name in DEFAULT_QUEUES
    )
    names = [queue.name for queue in queues]
    if len(names) != len(set(names)):
        raise ImproperlyConfiguredDbos(f"Duplicate queue names in {SETTING_NAME}['QUEUES'].")
    return queues


def resolve_settings() -> DbosSettings:
    """Build the final configuration from the ``DJANGO_DBOS`` setting."""
    queues = resolve_queues()
    return DbosSettings(
        app_name=resolve_app_name(),
        system_database_url=resolve_system_database_url(),
        application_version=get_dbos_setting('APPLICATION_VERSION'),
        dbos_system_schema=get_dbos_setting('SYSTEM_DB_SCHEMA'),
        executor_id=resolve_executor_id(),
        serializer=resolve_serializer(),
        queues=queues,
        capacity=resolve_capacity(),
    )
