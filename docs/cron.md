# Cron scheduling

Stack `@cron` on top of `@task` to run it on a schedule:

```python
from django.tasks import task
from django_dbos import cron


@cron('*/5 * * * *')  # every 5 minutes
@task
def cleanup_expired_sessions(): ...
```

The expression uses the standard 5-field crontab syntax. A 6-field form with a
leading seconds field is also accepted (DBOS supports sub-minute schedules).

Schedules are managed by DBOS, not by the Django process: the worker registers
them in the system database (idempotently, on `dbos worker` startup) and DBOS
fires them even across restarts and deployments. Each firing runs the task's
regular workflow (the one `.enqueue()` uses) as a child on the task's queue,
so scheduled and enqueued executions share a workflow name in the admin and
in `dbos status`, and every `@dbos_params` option — step retries,
`workflow_timeout`, `max_recovery_attempts` — applies to both:

```python
@cron('0 3 * * *')
@dbos_params(workflow_timeout=3600, step_retries_allowed=True)
@task
def nightly_report(): ...
```

!!! note
    Scheduled tasks must not require arguments. django-dbos validates this when
    the module loads.

Run `dbos worker` without `--no-scheduler` so it registers and fires schedules.

The code is the source of truth for schedules: on startup the worker creates
the missing ones, re-creates those whose crontab, timezone or queue changed,
and deletes the django-dbos schedules whose task no longer carries `@cron`.
Re-creating a schedule resets a pause made from the admin.

## Timezone

The crontab is evaluated in Django's default timezone
([`TIME_ZONE`](https://docs.djangoproject.com/en/stable/ref/settings/#time-zone))
when `USE_TZ` is enabled, otherwise in UTC.

## Sentry monitoring

Install the extra:

```bash
uv add django-dbos[sentry]
```

Scheduled tasks are then monitored with [Sentry cron
monitors](https://docs.sentry.io/product/crons/): the monitor is upserted by
Sentry on each check-in, so there is nothing to create in the UI. Its slug is
derived from the task's module path (`myapp.tasks.cleanup` →
`myapp-tasks-cleanup`; long paths are cut from the left to Sentry's 50
characters, keeping the function name).

Check-ins are sent by the worker around each *scheduled* firing, and whether
Sentry is active is looked up at that moment — initializing the SDK after
the task modules are imported, or only in the worker, is fine. An ad-hoc
`cleanup.enqueue()` of the same task is not a cron execution and sends no
check-in.

Options of `@cron`:

```python
@cron("*/5 * * * *", sentry_monitor_config=None)  # disable monitoring for this task
```

```python
@cron(
    "0 0 * * *",
    sentry_monitor_config={
        "schedule": {"type": "crontab", "value": "0 0 * * *"},
        "timezone": "Europe/Paris",
        "checkin_margin": 2,  # hours of tolerance
        "max_runtime": 2 * 60 * 60,  # alert if it runs longer
    },
)
```

By default (no `sentry_monitor_config`), the monitor configuration is derived
from the crontab. Sub-minute schedules cannot be represented as Sentry monitors
and are simply not monitored.

Without `sentry-sdk` installed, everything is a no-op.

See [`examples/cron_report`](https://github.com/lewoudar/django_dbos/tree/main/examples/cron_report)
for a runnable project built around `@cron`.
