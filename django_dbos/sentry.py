"""Optional Sentry cron monitor support for scheduled tasks.

Tasks scheduled with ``@cron`` can be monitored by `Sentry cron monitors
<https://docs.sentry.io/product/crons/>`_. The monitor configuration is
upserted by Sentry on each check-in, so there is nothing to create in the
Sentry UI beforehand.

Install the extra to enable it::

    uv add django-dbos[sentry]

Without ``sentry-sdk`` installed everything here is a no-op.
"""

from __future__ import annotations

import contextlib
import re
import string
from collections.abc import Iterator
from typing import Any

from .logging import logger
from .tasks import DBOSTask

# Sentry monitor config: {"schedule": {"type": ..., "value": ...}, "timezone": ...}
SentryMonitorConfig = dict[str, Any]

UNSET: Any = object()

_SLUG_ALLOWED = set(string.ascii_lowercase + string.digits + '-_')
_MAX_SLUG_LENGTH = 50


def is_sentry_enabled() -> bool:
    """Whether sentry-sdk is installed and a client is currently active."""
    try:
        import sentry_sdk
    except ImportError:
        return False
    try:
        return bool(sentry_sdk.get_client().is_active())
    except Exception:  # noqa: BLE001 - any SDK failure means "not monitored", never a crash
        return False


def monitor_slug(task_path: str) -> str:
    """Derive a valid Sentry monitor slug from a task's module path.

    ``myapp.tasks.cleanup`` becomes ``myapp-tasks-cleanup``. Slugs are capped
    at 50 characters; a long path is cut from the left so the function name
    (its most distinctive part) survives.
    """
    slug = task_path.lower().replace('.', '-')
    slug = ''.join(char if char in _SLUG_ALLOWED else '-' for char in slug)
    slug = re.sub(r'-{2,}', '-', slug).strip('-')
    return slug[-_MAX_SLUG_LENGTH:].lstrip('-')


def crontab_to_monitor_config(crontab: str) -> SentryMonitorConfig | None:
    """Build a Sentry monitor config for a crontab expression.

    Returns None when the schedule cannot be represented as a Sentry monitor,
    e.g. sub-minute schedules (DBOS supports a leading seconds field, Sentry
    does not).
    """
    from django.conf import settings
    from django.utils import timezone

    fields = crontab.split()
    if len(fields) == 6:
        if fields[0] != '0':
            return None  # sub-minute schedule
        fields = fields[1:]
    elif len(fields) != 5:
        return None

    return {
        'schedule': {'type': 'crontab', 'value': ' '.join(fields)},
        'timezone': str(timezone.get_default_timezone()) if getattr(settings, 'USE_TZ', True) else 'UTC',
    }


def resolve_monitor_config(
    task: DBOSTask,
    crontab: str,
    sentry_monitor_config: SentryMonitorConfig | Any | None = UNSET,
) -> SentryMonitorConfig | None:
    """The monitor configuration to use for a cron task, or None to not monitor it.

    ``None`` is returned when the caller explicitly passed
    ``sentry_monitor_config=None``, or when the schedule has no monitor
    representation (sub-minute crontabs).
    """
    if sentry_monitor_config is None:
        return None
    if sentry_monitor_config is UNSET or sentry_monitor_config == {}:
        monitor_config = crontab_to_monitor_config(crontab)
        if monitor_config is None:
            logger.debug(
                'crontab cannot be represented as a Sentry monitor, skipping',
                task_name=task.name,
                crontab=crontab,
            )
        return monitor_config
    return sentry_monitor_config


@contextlib.contextmanager
def monitor_cron_execution(task: DBOSTask, monitor_config: SentryMonitorConfig | None) -> Iterator[None]:
    """Report one scheduled execution of ``task`` as a Sentry cron check-in.

    Decided at execution time: Sentry is looked up when the schedule fires,
    in the worker, so it does not matter when or in which process the SDK was
    initialized. A no-op without a monitor config or without an active
    Sentry client. Only scheduled firings go through here — an ad-hoc
    ``task.enqueue()`` of the same task is not a cron execution.
    """
    if monitor_config is None or not is_sentry_enabled():
        yield
        return

    import sentry_sdk.crons

    with sentry_sdk.crons.monitor(monitor_slug(task.module_path), monitor_config=monitor_config):
        yield
