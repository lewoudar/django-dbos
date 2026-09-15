"""Scheduling Django tasks with cron expressions.

Stack ``@cron`` on top of ``@task``::

    from django.tasks import task
    from django_dbos import cron

    @cron("*/5 * * * *")
    @task
    def cleanup(): ...

Scheduled executions go through DBOS schedules: the worker process creates
them in the system database (idempotently, see ``create_cron_schedules``) and
DBOS fires them even if this process restarts.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

from croniter import croniter
from dbos import DBOS

from .exceptions import ImproperlyConfiguredDbos
from .logging import logger
from .sentry import UNSET, SentryMonitorConfig, monitor_cron_task
from .tasks import DBOSTask

SCHEDULE_PREFIX = 'django_dbos.schedules.'


@dataclass(frozen=True)
class CronRegistration:
    task: DBOSTask
    crontab: str
    sentry_monitor_config: Any


_registry: dict[str, CronRegistration] = {}


def normalize_crontab(crontab: str) -> str:
    """Normalize a 5-field crontab to DBOS' 6-field (leading seconds) form."""
    fields = crontab.split()
    if len(fields) == 5:
        return f'0 {crontab}'
    if len(fields) == 6:
        return crontab
    raise ImproperlyConfiguredDbos(f'Invalid crontab {crontab!r}: expected 5 fields (or 6 with leading seconds).')


def cron(
    crontab: str,
    *,
    sentry_monitor_config: SentryMonitorConfig | None = UNSET,
) -> Any:
    """Schedule a Django task on a crontab.

    Args:
        crontab: Standard 5-field crontab expression (a 6-field form with
            leading seconds is also accepted).
        sentry_monitor_config: Sentry cron monitor configuration. By default
            it is derived from the crontab; pass ``None`` to disable
            monitoring for this task, or a dict to configure the monitor
            manually (see ``sentry_sdk.crons.monitor``).
    """
    normalized = normalize_crontab(crontab)
    if not croniter.is_valid(normalized, second_at_beginning=True):
        raise ImproperlyConfiguredDbos(f'Invalid crontab {crontab!r}.')

    def wrapper(task: DBOSTask) -> DBOSTask:
        _validate_cron_task(task)
        monitored = monitor_cron_task(task, crontab, sentry_monitor_config)
        _registry[monitored.module_path] = CronRegistration(
            task=monitored,
            crontab=normalized,
            sentry_monitor_config=sentry_monitor_config,
        )
        logger.debug('registered cron task', task_name=task.name, crontab=normalized)
        return monitored

    return wrapper


def _validate_cron_task(task: DBOSTask) -> None:
    """Cron tasks run without arguments: reject required parameters."""
    import inspect

    signature = inspect.signature(task.func)
    skip = 1 if task.takes_context else 0
    parameters = list(signature.parameters.values())[skip:]
    for parameter in parameters:
        if parameter.default is inspect.Parameter.empty and parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            raise ImproperlyConfiguredDbos(
                f"Task '{task.name}' scheduled with @cron must not require "
                'arguments (found required parameter '
                f"'{parameter.name}')."
            )


def schedule_name_for_task(task: DBOSTask) -> str:
    return f'{SCHEDULE_PREFIX}{task.module_path}'


def register_cron_schedules() -> None:
    """Create (or update) the DBOS schedule for every registered cron task.

    Called by the ``dbos_worker`` command after DBOS launch.
    """
    for registration in _registry.values():
        name = schedule_name_for_task(registration.task)
        scheduled_workflow = _register_scheduled_workflow(registration.task)

        existing = DBOS.get_schedule(name)
        existing_schedule = (
            existing.get('schedule') if isinstance(existing, dict) else getattr(existing, 'schedule', None)
        )
        if existing is not None and existing_schedule == registration.crontab:
            continue
        if existing is not None:
            logger.info('updating existing cron schedule', schedule_name=name, crontab=registration.crontab)
            DBOS.delete_schedule(name)
        DBOS.create_schedule(
            schedule_name=name,
            workflow_fn=scheduled_workflow,
            schedule=registration.crontab,
            cron_timezone=str(_django_timezone()),
            queue_name=registration.task.queue_name,
        )
        logger.info(
            'created cron schedule',
            schedule_name=name,
            crontab=registration.crontab,
            queue=registration.task.queue_name,
        )


def _django_timezone() -> Any:
    from django.conf import settings
    from django.utils import timezone as dj_timezone

    if getattr(settings, 'USE_TZ', True):
        return dj_timezone.get_default_timezone()
    return 'UTC'


def _register_scheduled_workflow(task: DBOSTask) -> Any:
    """Register the workflow executed on each cron firing: the task body."""
    from dbos._dbos import _get_or_create_dbos_registry

    from .tasks_registry import aexecute_task_body, execute_task_body

    name = schedule_name_for_task(task)
    if name in _get_or_create_dbos_registry().workflow_info_map:
        return _get_workflow(name)

    if inspect.iscoroutinefunction(task.func):

        @DBOS.workflow(name=name)
        async def scheduled_workflow(scheduled_time: Any, context: Any) -> Any:
            return await aexecute_task_body(task, (), {})

    else:

        @DBOS.workflow(name=name)
        def scheduled_workflow(scheduled_time: Any, context: Any) -> Any:
            return execute_task_body(task, (), {})

    return _get_workflow(name)


def _get_workflow(name: str) -> Any:
    from dbos._dbos import _get_or_create_dbos_registry

    workflow = _get_or_create_dbos_registry().workflow_info_map.get(name)
    if workflow is None:  # pragma: no cover - registered just above
        raise RuntimeError(f'Workflow {name} was not registered')
    return workflow
