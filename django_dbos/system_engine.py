"""The SQLAlchemy engine handed to DBOS for a CockroachDB system database.

Given a URL alone, DBOS forces the ``postgresql+psycopg`` driver, whose
PostgreSQL dialect cannot connect to CockroachDB. The worker and the client
therefore receive an engine built here from the ``cockroachdb+psycopg2`` URL,
with LISTEN/NOTIFY off (CockroachDB has none). Other system databases need
nothing: DBOS builds its own engine.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from . import settings as settings_module
from .exceptions import ImproperlyConfiguredDbos


def create_engine(url: str) -> sa.Engine:
    """A SQLAlchemy engine for a ``cockroachdb+psycopg2`` URL, if the extra is installed."""
    try:
        import psycopg2  # noqa: F401 - the driver DBOS needs on CockroachDB
        import sqlalchemy_cockroachdb  # noqa: F401 - registers the cockroachdb dialect
    except ImportError as exc:
        raise ImproperlyConfiguredDbos(
            f'A CockroachDB system database needs the django-dbos[cockroachdb] extra ({exc}).'
        ) from None
    return sa.create_engine(url)


def system_database_options(url: str) -> dict[str, Any]:
    """Extra ``DBOS`` config / ``DBOSClient`` arguments for the system database at ``url``.

    For CockroachDB, a new engine the caller disposes of (DBOS never does):
    ``system_database_engine`` and ``use_listen_notify=False``. Empty otherwise.
    """
    if not settings_module.is_cockroachdb_url(url):
        return {}
    return {'system_database_engine': create_engine(url), 'use_listen_notify': False}


def dispose(options: dict[str, Any]) -> None:
    """Dispose of the engine ``system_database_options`` created, if any."""
    engine = options.get('system_database_engine')
    if engine is not None:
        engine.dispose()
