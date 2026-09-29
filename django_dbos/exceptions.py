"""django-dbos exceptions and error-message hygiene."""

from __future__ import annotations

import re


class DjangoDbosError(Exception):
    """Base class for django-dbos errors."""


class ImproperlyConfiguredDbos(DjangoDbosError):
    """The DJANGO_DBOS setting or environment is misconfigured."""


class QueueNotDeclared(DjangoDbosError):
    """The task was enqueued on a queue that is not declared."""


class TaskCancelled(DjangoDbosError):
    """The task was cancelled before completing (admin, ``DBOSClient.cancel_workflow``).

    Reported in ``TaskResult.errors`` of a ``FAILED`` result: Django has no
    cancelled status of its own.
    """


class TaskTimedOut(TaskCancelled):
    """The task was cancelled by DBOS because it exceeded its ``workflow_timeout``."""


class MaxRecoveryAttemptsExceeded(DjangoDbosError):
    """The task was interrupted (worker crashes) more times than ``max_recovery_attempts`` allows.

    DBOS stops recovering it; reported in ``TaskResult.errors`` of a ``FAILED`` result.
    """


#: A DSN with credentials embedded in an error message, e.g.
#: ``postgresql+psycopg://user:secrets@host/db``. Drivers and ORMs may include
#: one in connection errors; the password is masked before showing the message.
_DSN_WITH_PASSWORD_RE = re.compile(r"([a-z][a-z0-9+.-]*://[^\s:/@\"']+):([^\s@\"'/]+)@", re.IGNORECASE)


def scrub_credentials(message: str) -> str:
    """Mask passwords in any DSN found in an error message."""
    return _DSN_WITH_PASSWORD_RE.sub(r'\1:***@', message)


def describe_error(exc: BaseException) -> str:
    """``TypeName: message`` with credentials masked — safe for pages, logs and CLI output."""
    return f'{type(exc).__name__}: {scrub_credentials(str(exc))}'


def system_database_errors() -> tuple[type[BaseException], ...]:
    """Exception types raised when the DBOS system database is unreachable or rejects an operation.

    ``DBAPIError`` is SQLAlchemy's base for driver errors (connection refused,
    authentication failed, ...); ``DBOSException`` covers the SDK's own
    operational errors (invalid schedule, unknown queue, ...).
    """
    from dbos import error as dbos_error
    from sqlalchemy.exc import DBAPIError

    return (DBAPIError, dbos_error.DBOSException)
