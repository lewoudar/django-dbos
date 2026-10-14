"""Helpers shared by the test suite."""

from __future__ import annotations

from typing import Any

from sentry_sdk.transport import Transport

from django_dbos.tasks import DBOSTask


def noop() -> None: ...


def set_dbos_options(settings: Any, options: dict[str, Any]) -> None:
    """Replace the ``OPTIONS`` of the test DBOS backend through pytest-django's ``settings`` fixture."""
    settings.TASKS = {**settings.TASKS, 'dbos': {**settings.TASKS['dbos'], 'OPTIONS': options}}


def make_dbos_task(**overrides: Any) -> DBOSTask:
    """Build a DBOSTask from a module-level test function."""
    return DBOSTask(func=noop, **overrides)


class MemoryTransport(Transport):
    """In-memory Sentry transport collecting events and envelopes."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.envelopes: list[Any] = []

    def capture_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def capture_envelope(self, envelope: Any) -> None:
        self.envelopes.append(envelope)
