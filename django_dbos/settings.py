"""Configuration for django-dbos.

Everything is driven by the ``OPTIONS`` of the ``TASKS`` entry using
``DBOSBackend``: a single one per project, DBOS being a per-process singleton.
Options not set there fall back to environment variables where DBOS supports
them (``DBOS_SYSTEM_DATABASE_URL``, ...). When no URL is given at all, the
system database URL is derived from the default Django database connection:

- Postgres: same server, dedicated database ``<SYSTEM_DB_PREFIX><dbname>``
  (created with DBOS' migrations by ``manage.py dbos migrate``, or on worker
  launch unless ``RUN_MIGRATIONS`` is ``False``).
- CockroachDB: same, through the ``cockroachdb+psycopg2`` driver (see
  ``system_engine``).
- SQLite: a file next to the Django database.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from dbos import Serializer
from django.conf import settings
from django.tasks.base import DEFAULT_TASK_QUEUE_NAME
from django.utils.module_loading import import_string

from .exceptions import ImproperlyConfiguredDbos

DEFAULT_SYSTEM_DB_PREFIX = 'dbos_'

# DBOS needs psycopg2 on CockroachDB: psycopg 3 sends typed parameters
# (``now() * 1000::INTEGER``), which CockroachDB rejects (float * int).
COCKROACHDB_DRIVERS = ('cockroachdb', 'cockroachdb+psycopg2')

# Every ``OPTIONS`` key django-dbos accepts; the system checks report any other one.
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
    'MIGRATE_WITH_DJANGO',
    'SCHEDULER_POLLING_INTERVAL_SEC',
    'ADMIN_ENABLED',
    'ADMIN_SITE',
)


def dbos_backend_aliases() -> list[str]:
    """The ``TASKS`` aliases whose backend is ``DBOSBackend`` (or a subclass of it)."""
    from .backends import DBOSBackend  # the backend module imports this one

    aliases: list[str] = []
    for alias, params in (getattr(settings, 'TASKS', None) or {}).items():
        try:
            backend_class = import_string(params['BACKEND'])
        except (ImportError, KeyError, TypeError):
            continue  # an invalid backend is Django's own checks' business
        if isinstance(backend_class, type) and issubclass(backend_class, DBOSBackend):
            aliases.append(alias)
    return aliases


def _backend_alias(alias: str | None = None) -> str | None:
    """``alias`` when given, else the DBOS backend's: the first one if several (see ``django_dbos.E002``)."""
    if alias is not None:
        return alias
    aliases = dbos_backend_aliases()
    return aliases[0] if aliases else None


def _dbos_backend_params(alias: str | None = None) -> dict[str, Any]:
    """The ``TASKS`` entry of the DBOS backend (empty when there is none)."""
    alias = _backend_alias(alias)
    return settings.TASKS[alias] if alias is not None else {}


def backend_path(alias: str | None = None) -> str:
    """The DBOS backend's ``TASKS`` entry, for messages: ``TASKS['dbos']``."""
    alias = _backend_alias(alias)
    return f'TASKS[{alias!r}]' if alias is not None else 'TASKS[<alias>]'


def options_path(key: str | None = None, alias: str | None = None) -> str:
    """Where an option lives, for messages: ``TASKS['dbos']['OPTIONS']['SERIALIZER']``."""
    path = f"{backend_path(alias)}['OPTIONS']"
    return path if key is None else f'{path}[{key!r}]'


def _validate_options() -> None:
    options = _dbos_backend_params().get('OPTIONS')
    if options is not None and not isinstance(options, dict):
        raise ImproperlyConfiguredDbos(f'{options_path()} must be a mapping, got {type(options).__name__}.')


def get_dbos_options(alias: str | None = None) -> dict[str, Any]:
    """The ``OPTIONS`` of the DBOS backend (empty when there is none).

    ``OPTIONS`` that are not a mapping read as empty, so that the admin and the
    system checks keep working: ``resolve_settings`` reports them
    (``django_dbos.E001``) and the client and worker refuse to start.
    """
    options = _dbos_backend_params(alias).get('OPTIONS')
    return options if isinstance(options, dict) else {}


def get_dbos_setting(key: str, default: Any = None) -> Any:
    """Read a key of the DBOS backend's ``OPTIONS``."""
    return get_dbos_options().get(key, default)


@dataclass(frozen=True)
class QueueConfig:
    """Configuration of a DBOS queue declared from Django settings."""

    name: str
    worker_concurrency: int | None = None
    global_concurrency: int | None = None
    limiter: dict[str, Any] | None = None
    polling_interval_sec: float | None = None
    partition_concurrency: int | None = None
    partition_worker_concurrency: int | None = None
    partition_limiter: dict[str, Any] | None = None
    # Per-queue capacity thresholds (django-dbos only, never sent to DBOS),
    # same keys as ``OPTIONS["CAPACITY"]`` minus ``ADVISOR``.
    capacity: dict[str, Any] | None = None

    @property
    def partitioned(self) -> bool:
        """Whether a per-partition limit is set, so that every enqueue needs a partition key."""
        return any(getattr(self, name) is not None for name in PARTITION_OPTIONS)


# The ``QueueConfig`` options that make a queue partitioned.
PARTITION_OPTIONS = ('partition_concurrency', 'partition_worker_concurrency', 'partition_limiter')


# ``OPTIONS["CAPACITY"]`` threshold keys and their defaults; see ``capacity.advise_queue``.
CAPACITY_THRESHOLD_DEFAULTS: dict[str, float | int] = {
    'SCALE_UP_BACKLOG_AGE_SECONDS': 60.0,
    'SCALE_DOWN_MAX_RUNNING': 0,
    # Floor of live workers per queue (SCALE_UP below it, no SCALE_DOWN at it):
    # an idle worker is what fires the @cron schedules and picks up new tasks.
    'MIN_WORKERS': 1,
}
# Thresholds that count workflows or workers: whole numbers only.
INTEGER_THRESHOLDS = frozenset({'SCALE_DOWN_MAX_RUNNING', 'MIN_WORKERS'})


@dataclass(frozen=True)
class CapacityConfig:
    """Resolved ``OPTIONS["CAPACITY"]``: global thresholds and optional custom advisor."""

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
            f'{options_path("SYSTEM_DATABASE_URL")} / DBOS_SYSTEM_DATABASE_URL.'
        )
    return default_db


def _sqlite_system_database_url(django_db: dict[str, Any]) -> str:
    name = django_db['NAME']
    if name == ':memory:':
        return 'sqlite:///:memory:'
    return f'sqlite:///{name}-dbos.sqlite'


def _server_system_database_url(django_db: dict[str, Any], *, driver: str, default_port: str) -> str:
    """A dedicated ``<SYSTEM_DB_PREFIX><dbname>`` database on the Django database's server."""
    prefix = get_dbos_setting('SYSTEM_DB_PREFIX', DEFAULT_SYSTEM_DB_PREFIX)
    # percent-encode the credentials (a password may contain '@', ':' or '/')
    user = quote(django_db.get('USER') or '', safe='')
    password = quote(django_db.get('PASSWORD') or '', safe='')
    host = django_db.get('HOST') or 'localhost'
    port = django_db.get('PORT') or default_port
    return f'{driver}://{user}:{password}@{host}:{port}/{prefix}{django_db["NAME"]}'


def derive_system_database_url(django_db: dict[str, Any]) -> str:
    """Derive the DBOS system database URL from a Django database config dict."""
    engine = django_db.get('ENGINE') or ''
    if 'sqlite' in engine:
        return _sqlite_system_database_url(django_db)
    if 'cockroach' in engine:  # django-cockroachdb's 'django_cockroachdb'
        return _server_system_database_url(django_db, driver='cockroachdb+psycopg2', default_port='26257')
    if 'postgres' in engine:
        return _server_system_database_url(django_db, driver='postgresql+psycopg', default_port='5432')
    raise ImproperlyConfiguredDbos(
        f"Unsupported database engine '{engine}' for deriving the DBOS system "
        'database. Set '
        f'{options_path("SYSTEM_DATABASE_URL")} or DBOS_SYSTEM_DATABASE_URL explicitly.'
    )


def resolve_system_database_url() -> str:
    """Resolve the DBOS system database URL.

    By order of precedence:

    1. ``OPTIONS["SYSTEM_DATABASE_URL"]``
    2. ``DBOS_SYSTEM_DATABASE_URL`` environment variable
    3. Derived from the default Django database connection
    """
    url = get_dbos_setting('SYSTEM_DATABASE_URL') or os.environ.get('DBOS_SYSTEM_DATABASE_URL')
    if not url:
        return derive_system_database_url(_django_database_settings())
    driver = url.partition('://')[0]
    if is_cockroachdb_url(url) and driver not in COCKROACHDB_DRIVERS:
        raise ImproperlyConfiguredDbos(
            f"The '{driver}' driver cannot reach a CockroachDB system database: DBOS needs psycopg2 there. "
            "Use a 'cockroachdb+psycopg2://' URL."
        )
    return url


def is_cockroachdb_url(url: str) -> bool:
    """Whether the system database URL targets CockroachDB (``cockroachdb[+driver]://``)."""
    return url.startswith('cockroachdb')


# Boolean options and their defaults (see ``get_bool_option``).
BOOLEAN_OPTIONS: dict[str, bool] = {
    # Whether workers create/migrate the system database on launch.
    'RUN_MIGRATIONS': True,
    # Whether ``manage.py migrate`` also migrates the system database.
    'MIGRATE_WITH_DJANGO': True,
    # Whether the DBOS pages are registered in the Django admin.
    'ADMIN_ENABLED': True,
}


def get_bool_option(key: str) -> bool:
    """A boolean option, its default when unset.

    Only ``True`` and ``False`` are accepted: ``bool()`` would read ``"false"``
    (say, from an environment variable) as true.
    """
    default = BOOLEAN_OPTIONS[key]  # first: an unknown key is a bug, whatever the settings
    value = get_dbos_setting(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ImproperlyConfiguredDbos(f'{options_path(key)} must be True or False, got {value!r}.')
    return value


def resolve_app_name() -> str:
    """Application name for DBOS, defaulting to the Django project name."""
    return str(get_dbos_setting('NAME') or os.environ.get('DBOS_APPLICATION_NAME') or 'django')


def resolve_executor_id() -> str | None:
    """Identity of this worker process for DBOS (``executor_id``).

    Resolved from ``OPTIONS["EXECUTOR_ID"]``, then the ``DBOS__VMID``
    environment variable DBOS itself honours; ``None`` leaves DBOS' default
    (``"local"``). Workflow rows, recovery on restart and worker heartbeats
    are all keyed by it, so give each worker instance a *stable* identity
    (e.g. ``FLY_MACHINE_ID`` on fly.io, the pod name on Kubernetes).
    """
    executor_id = get_dbos_setting('EXECUTOR_ID') or os.environ.get('DBOS__VMID')
    return str(executor_id) if executor_id else None


def resolve_serializer() -> Any:
    """The DBOS serializer for task arguments, results and errors, or ``None``.

    ``OPTIONS["SERIALIZER"]`` is a ``dbos.Serializer`` instance, a class
    (instantiated without arguments) or the dotted path of either. ``None``
    keeps DBOS' default, which is **pickle**: anyone who can write to the
    system database can then run code in the workers and in every process
    reading results. ``dbos.DBOSPortableJSONSerializer`` is the safe
    alternative when task arguments and results are plain data.

    The worker and every client must use the same serializer.
    """
    value = get_dbos_setting('SERIALIZER')
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = import_string(value)
        except ImportError as exc:
            raise ImproperlyConfiguredDbos(f'{options_path("SERIALIZER")}: {exc}') from exc
    if isinstance(value, type):
        value = value()
    if not isinstance(value, Serializer):
        raise ImproperlyConfiguredDbos(
            f'{options_path("SERIALIZER")} must be a dbos.Serializer instance, subclass or dotted path.'
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
        if key in INTEGER_THRESHOLDS and not isinstance(value, int):
            raise ImproperlyConfiguredDbos(f'{where}[{key!r}] must be a non-negative integer, got {value!r}.')
        thresholds[key] = value
    return thresholds


def resolve_capacity() -> CapacityConfig:
    """Resolve ``OPTIONS["CAPACITY"]``: thresholds of the scaling advice and an optional advisor.

    ``ADVISOR`` is a callable ``(queue_name, metrics, thresholds) -> str`` (or
    its dotted path) replacing the default heuristic, ``capacity.advise_queue``.
    """
    where = options_path('CAPACITY')
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


def _queue_config(name: str, options: Any, where: str) -> QueueConfig:
    if options is None:
        return QueueConfig(name=name)
    if not isinstance(options, dict):
        raise ImproperlyConfiguredDbos(
            f"{where}['{name}'] must be a mapping of options (or None), got {type(options).__name__}."
        )
    unknown = sorted(set(options) - set(_QUEUE_OPTIONS))
    if unknown:
        raise ImproperlyConfiguredDbos(
            f"{where}['{name}']: unknown option(s) {', '.join(unknown)}. Valid options: {', '.join(_QUEUE_OPTIONS)}."
        )
    if options.get('capacity') is not None:
        options = {
            **options,
            'capacity': _validate_capacity_thresholds(options['capacity'], f"{where}['{name}']['capacity']"),
        }
    return QueueConfig(name=name, **options)


def _declared_queues(alias: str | None) -> dict[Any, Any]:
    """Queue name -> options, from ``OPTIONS["QUEUES"]`` or the ``QUEUES`` list of the backend's entry."""
    params = _dbos_backend_params(alias)
    queue_options = get_dbos_options(alias).get('QUEUES')
    if queue_options is None:
        names = params.get('QUEUES', [DEFAULT_TASK_QUEUE_NAME])
        if isinstance(names, str) or not isinstance(names, list | tuple | set | frozenset):
            raise ImproperlyConfiguredDbos(
                f"{backend_path(alias)}['QUEUES'] must be a list of queue names, got {names!r}."
            )
        if not names:
            raise ImproperlyConfiguredDbos(
                f"{backend_path(alias)}['QUEUES'] is empty, which lets tasks target any queue, but a DBOS worker "
                'only processes the queues it declares: list them.'
            )
        return dict.fromkeys(names)

    if 'QUEUES' in params:
        raise ImproperlyConfiguredDbos(
            f"Declare the queues either in {backend_path(alias)}['QUEUES'] (names only) or in "
            f'{options_path("QUEUES", alias)} (names and options), not both.'
        )

    if not isinstance(queue_options, dict) or not queue_options:
        raise ImproperlyConfiguredDbos(
            f'{options_path("QUEUES", alias)} must be a non-empty mapping of queue name to options.'
        )
    return queue_options


def resolve_queues(alias: str | None = None) -> tuple[QueueConfig, ...]:
    """Resolve the DBOS queues declared by the ``TASKS`` entry ``alias`` (the DBOS backend's by default).

    Queues are declared once, either as a list of names in ``QUEUES`` (no
    options) or as a mapping of name to options (``None`` for none) in
    ``OPTIONS["QUEUES"]``; ``["default"]`` when neither is set. The backend
    accepts exactly these queues, and the worker processes them.
    """
    alias = _backend_alias(alias)
    where = options_path('QUEUES', alias)
    queues = tuple(_queue_config(str(name), options, where) for name, options in _declared_queues(alias).items())
    names = [queue.name for queue in queues]
    if len(names) != len(set(names)):
        raise ImproperlyConfiguredDbos(f'Duplicate queue names in {where}.')
    return queues


def resolve_settings() -> DbosSettings:
    """Build the final configuration from the DBOS backend's ``OPTIONS``."""
    _validate_options()
    for key in BOOLEAN_OPTIONS:
        get_bool_option(key)
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
