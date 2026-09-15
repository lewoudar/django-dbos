"""Configuration for django-dbos.

Everything is driven by a single ``DJANGO_DBOS`` setting, a dict of options.
Options not set there fall back to environment variables where DBOS supports
them (``DBOS_SYSTEM_DATABASE_URL``, ...). When no URL is given at all, the
system database URL is derived from the default Django database connection:

- Postgres: same server, dedicated database ``<DBOS_SCHEMA_PREFIX><dbname>``
  (DBOS creates it and runs its migrations on launch).
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
    return QueueConfig(name=name, **options)


def resolve_queues() -> tuple[QueueConfig, ...]:
    """Resolve declared DBOS queues.

    Users declare extra queues in ``DJANGO_DBOS["QUEUES"]`` as a mapping of
    queue name to queue options. The Django tasks backend also creates queues
    on the fly for any queue listed in ``TASKS[alias]["QUEUES"]``.
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
    )
