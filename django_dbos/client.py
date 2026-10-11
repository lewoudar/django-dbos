"""Access to the DBOS system database from processes that are not workers.

DBOS distinguishes two kinds of processes:

- the *application* (``DBOS`` singleton + ``DBOS.launch()``): registers
  workflow functions, dequeues and executes them, runs the scheduler and the
  recovery of interrupted workflows. In django-dbos, that is ``dbos worker``.
- *clients* (``dbos.DBOSClient``): talk to the system database only — enqueue
  workflows by name, read statuses, cancel/resume/fork, manage schedules.
  They never run workflow code.

Everything outside the worker (web processes enqueuing tasks and reading
results, the admin pages, ``dbos status``) goes through the process-wide
client returned by ``get_client``. That keeps a web process from
launching DBOS' background threads — which would turn it into a worker.
"""

from __future__ import annotations

import threading
from typing import Any

from dbos import DBOSClient

from . import settings as settings_module
from .exceptions import ImproperlyConfiguredDbos, describe_error

_client_lock = threading.Lock()
_client: DBOSClient | None = None


def get_client() -> DBOSClient:
    """The process-wide ``dbos.DBOSClient``, created on first use.

    The client connects lazily: a misconfigured or unreachable system database
    surfaces on the first operation, not at import or ``ready()`` time, and
    connection errors are raised rather than retried (see
    ``exceptions.system_database_errors``).
    """
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            _client = _create_client()
        return _client


def _create_client() -> DBOSClient:
    try:
        resolved = settings_module.resolve_settings()
        kwargs: dict[str, Any] = {}
        if resolved.serializer is not None:
            kwargs['serializer'] = resolved.serializer
        return DBOSClient(
            system_database_url=resolved.system_database_url,
            dbos_system_schema=resolved.dbos_system_schema or 'dbos',
            application_name=resolved.app_name,
            lazy=True,
            # A web request must fail fast when the system database is down;
            # the SDK default retries forever (the right choice for a worker).
            retry_connection_errors=False,
            **kwargs,
        )
    except ImproperlyConfiguredDbos:
        raise
    except Exception as exc:  # noqa: BLE001 - deliberately unchained: the cause would print the DSN
        raise ImproperlyConfiguredDbos(describe_error(exc)) from None


def close_client() -> None:
    """Dispose of the process-wide client (tests, configuration changes)."""
    global _client
    with _client_lock:
        if _client is not None:
            _client.destroy()
            _client = None
