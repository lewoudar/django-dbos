"""Scheduling Django tasks with cron expressions.

Stack ``@cron`` on top of ``@task``:

    from django.tasks import task
    from django_dbos import cron

    @cron("*/5 * * * *")
    @task
    def cleanup(): ...

Scheduled executions go through DBOS schedules: every worker registers the
scheduled workflows before launch (``register_cron_workflows``), then
reconciles the schedules in the system database with the code
(``register_cron_schedules``). DBOS fires them from any running worker, even
across restarts.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

from croniter import croniter
from dbos import DBOS, SetWorkflowTimeout
from django.conf import settings
from django.utils import timezone as dj_timezone

from .exceptions import ImproperlyConfiguredDbos
from .logging import logger
from .sentry import UNSET, SentryMonitorConfig, monitor_cron_execution, resolve_monitor_config
from .tasks import DBOSTask
from .tasks_registry import registered_workflow, remember_workflow, task_workflow

SCHEDULE_PREFIX = 'django_dbos.schedules.'


@dataclass(frozen=True)
class CronRegistration:
    task: DBOSTask
    crontab: str
    # Resolved Sentry monitor configuration (None: not monitored).
    monitor_config: SentryMonitorConfig | None


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

    Returns:
        A decorator registering the task, to stack on top of ``@task``.

    Raises:
        ImproperlyConfiguredDbos: The crontab is invalid, or the task requires
            arguments (cron tasks run without any).
    """
    normalized = normalize_crontab(crontab)
    if not croniter.is_valid(normalized, second_at_beginning=True):
        raise ImproperlyConfiguredDbos(f'Invalid crontab {crontab!r}.')

    def wrapper(task: DBOSTask) -> DBOSTask:
        _validate_cron_task(task)
        _registry[task.module_path] = CronRegistration(
            task=task,
            crontab=normalized,
            monitor_config=resolve_monitor_config(task, crontab, sentry_monitor_config),
        )
        logger.debug('registered cron task', task_name=task.name, crontab=normalized)
        return task

    return wrapper


def _validate_cron_task(task: DBOSTask) -> None:
    """Cron tasks run without arguments: reject required parameters."""
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


def register_cron_workflows() -> None:
    """Register the workflow fired by each ``@cron`` task's schedule.

    Called for every worker before ``DBOS.launch()``: DBOS runs its scheduler
    in every launched process, so any worker may fire a schedule and dequeue
    the firing — and can only execute it if the workflow is registered here.
    """
    for registration in _registry.values():
        _register_scheduled_workflow(registration.task, registration.monitor_config)


def register_cron_schedules() -> None:
    """Reconcile the DBOS schedules with the ``@cron`` tasks of this process.

    Called by the ``dbos worker`` command after DBOS launch. The code is the
    source of truth: a schedule is created for every cron task, re-created
    when its crontab, timezone or queue changed, and deleted when its task no
    longer carries ``@cron`` (an orphan would keep firing a workflow no worker
    registers). Re-creating a schedule also resets a pause made in the admin.
    """
    cron_timezone = str(_django_timezone())
    for registration in _registry.values():
        name = schedule_name_for_task(registration.task)
        scheduled_workflow = _register_scheduled_workflow(registration.task, registration.monitor_config)
        wanted = {
            'schedule': registration.crontab,
            'cron_timezone': cron_timezone,
            'queue_name': registration.task.queue_name,
        }

        existing = DBOS.get_schedule(name)
        if existing is not None:
            current = {key: existing.get(key) for key in wanted}
            if current == wanted:
                continue
            logger.info('updating existing cron schedule', schedule_name=name, previous=current, **wanted)
            DBOS.delete_schedule(name)
        DBOS.create_schedule(schedule_name=name, workflow_fn=scheduled_workflow, **wanted)
        logger.info('created cron schedule', schedule_name=name, **wanted)

    _delete_orphan_schedules()


def _delete_orphan_schedules() -> None:
    """Delete django-dbos schedules whose task no longer carries ``@cron``."""
    wanted = {schedule_name_for_task(registration.task) for registration in _registry.values()}
    for schedule in DBOS.list_schedules(schedule_name_prefix=SCHEDULE_PREFIX):
        name = schedule['schedule_name']
        if name in wanted:
            continue
        DBOS.delete_schedule(name)
        logger.info('deleted orphan cron schedule', schedule_name=name, crontab=schedule.get('schedule'))


def _django_timezone() -> Any:
    if getattr(settings, 'USE_TZ', True):
        return dj_timezone.get_default_timezone()
    return 'UTC'


def _register_scheduled_workflow(task: DBOSTask, monitor_config: SentryMonitorConfig | None = None) -> Any:
    """Register the workflow fired by the schedule.

    It is a thin parent: each firing runs the task's regular workflow (the one
    enqueues use, ``django_dbos.tasks.<module path>``) as a child, so cron and
    enqueued executions share a workflow name — and the task's DBOS options
    (``workflow_timeout``, ``max_recovery_attempts``, step retries) apply to
    both. ``SetWorkflowTimeout`` is how DBOS scopes a timeout to a child
    started from a workflow; schedules themselves carry none. The parent is
    also where a firing is reported to Sentry, so ad-hoc enqueues are not.
    """
    name = schedule_name_for_task(task)
    existing = registered_workflow(name)
    if existing is not None:
        return existing

    child = task_workflow(task)
    timeout = task.dbos_params.workflow_timeout

    if inspect.iscoroutinefunction(task.func):

        @DBOS.workflow(name=name)
        async def scheduled_workflow(scheduled_time: Any, context: Any) -> Any:
            with monitor_cron_execution(task, monitor_config), SetWorkflowTimeout(timeout):
                return await child()

    else:

        @DBOS.workflow(name=name)
        def scheduled_workflow(scheduled_time: Any, context: Any) -> Any:
            with monitor_cron_execution(task, monitor_config), SetWorkflowTimeout(timeout):
                return child()

    return remember_workflow(name, scheduled_workflow)
