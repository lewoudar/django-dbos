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

import re
import string
from dataclasses import replace
from typing import Any

from .logging import logger
from .tasks import DBOSTask

# Sentry monitor config: {"schedule": {"type": ..., "value": ...}, "timezone": ...}
SentryMonitorConfig = dict[str, Any]

UNSET: Any = object()

_SLUG_ALLOWED = set(string.ascii_lowercase + string.digits + "-_")
_MAX_SLUG_LENGTH = 50


def is_sentry_enabled() -> bool:
    """Whether sentry-sdk is installed and a client is currently active."""
    try:
        import sentry_sdk
    except ImportError:
        return False
    try:
        return bool(sentry_sdk.get_client().is_active())
    except Exception:  # noqa: BLE001 - never fail task execution on monitoring
        return False


def monitor_slug(task_name: str) -> str:
    """Derive a valid Sentry monitor slug from a task name."""
    slug = task_name.lower().replace(".", "-")
    slug = "".join(char if char in _SLUG_ALLOWED else "-" for char in slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug[:_MAX_SLUG_LENGTH]


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
        if fields[0] != "0":
            return None  # sub-minute schedule
        fields = fields[1:]
    elif len(fields) != 5:
        return None

    return {
        "schedule": {"type": "crontab", "value": " ".join(fields)},
        "timezone": str(timezone.get_default_timezone()) if getattr(settings, "USE_TZ", True) else "UTC",
    }


def monitor_cron_task(
    task: DBOSTask,
    crontab: str,
    sentry_monitor_config: SentryMonitorConfig | Any | None = UNSET,
) -> DBOSTask:
    """Wrap the task function with a Sentry cron monitor when possible.

    The task is returned unchanged when sentry-sdk is not active, when the
    schedule has no monitor representation, or when the caller explicitly
    passed ``sentry_monitor_config=None``.
    """
    if sentry_monitor_config is None or not is_sentry_enabled():
        return task

    if sentry_monitor_config is UNSET or sentry_monitor_config == {}:
        monitor_config = crontab_to_monitor_config(crontab)
        if monitor_config is None:
            logger.debug(
                "crontab cannot be represented as a Sentry monitor, skipping",
                task_name=task.name,
                crontab=crontab,
            )
            return task
    else:
        monitor_config = sentry_monitor_config

    import sentry_sdk.crons

    slug = monitor_slug(task.name)
    monitored = sentry_sdk.crons.monitor(slug, monitor_config=monitor_config)(task.func)
    return replace(task, func=monitored)
