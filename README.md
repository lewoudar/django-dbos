# django-dbos

**⚠️ Experimental project. Do not use in production.**

A [DBOS](https://docs.dbos.dev/)-backed backend for Django's
[tasks framework](https://docs.djangoproject.com/en/stable/topics/tasks/):
background tasks that survive a worker crash, cron scheduling, optional
Sentry monitoring, an admin UI to inspect and operate on tasks, and a worker
capacity report to drive autoscaling.

## At a glance

If you know Django's tasks API, you already know django-dbos:

```python
from django.tasks import task


@task
def send_welcome_email(user_id: int) -> None: ...


result = send_welcome_email.enqueue(user_id)  # durable by default
result.status  # READY, RUNNING, SUCCESSFUL, FAILED — standard Django TaskResult
```

Each enqueued task is recorded in a database before it runs. If the worker
executing it crashes, the task is not lost: it runs again once a worker is
back.

## Why DBOS

Say you send a newsletter to 10,000 users:

```python
from django.tasks import task


@task
def send_newsletter(campaign_id: int) -> None:
    for user_id in User.objects.values_list('id', flat=True):
        send_one_email(user_id, campaign_id)
```

With most task backends, if the worker is killed at email #6,000 — a deploy,
an OOM, a spot instance reclaimed — the retry restarts the task from #1.
6,000 people get the same email twice.

With django-dbos, one option and one call per email turn the loop into a
durable, checkpointed workflow:

```python
from django.tasks import task
from django_dbos import dbos_params, get_dbos_context


@dbos_params(durable_operations=True)
@task
def send_newsletter(campaign_id: int) -> None:
    ctx = get_dbos_context()
    for user_id in User.objects.values_list('id', flat=True):
        ctx.run_step(send_one_email, user_id, campaign_id, name=f'email-{user_id}')
```

Each `run_step` call is recorded in the system database as it completes.
Kill the worker at #6,000, restart it, and the task resumes from there: the
5,999 emails already sent are skipped, and only the one in flight at the
moment of the crash may go out again. No external state to reconcile, no
idempotency keys to invent yourself.

Cron schedules are just `@task`s with a firing time attached — same
durability, same retries, same admin page:

```python
from django_dbos import cron


@cron('0 3 * * *')  # every day at 3am
@dbos_params(workflow_timeout=3600, step_retries_allowed=True)
@task
def nightly_report() -> None: ...
```

Schedules are stored in the system database, so they keep firing across
restarts and deploys — and with the `sentry` extra installed, each firing
checks in with [Sentry Crons](https://docs.sentry.io/product/crons/)
automatically.

## What you get

- **Durable tasks** — every `.enqueue()` becomes a DBOS workflow: automatic
  recovery after a crash, step retries with backoff, deferred execution
  (`run_after`), deduplication, priorities and workflow timeouts, all through
  Django's own `Task` / `TaskResult` API.
- **Durable workflows** *(opt-in)* — checkpointed steps, durable sleeps that
  survive restarts, progress events, messages between workflows and child
  workflows, from inside a task.
- **Cron scheduling** — `@cron('0 3 * * *')` on top of `@task`. Every worker
  fires the schedules, each firing runs once, and no separate scheduler
  process is needed.
- **Sentry cron monitoring** *(optional)* — scheduled tasks check in with
  [Sentry Crons](https://docs.sentry.io/product/crons/) automatically, no
  monitor to configure by hand.
- **Admin pages** — inspect every workflow (inputs, outputs, errors, step
  history), and cancel, resume, fork or delete them; manage schedules; see
  which workers are alive. Superuser-only.
- **Worker capacity report** — `dbos status` (or `capacity.collect()` from
  Python) reports backlog, running count and per-worker load per queue, with
  a scale up/down/OK verdict you can plug into an autoscaler.
- **Structured logging** — everything goes through
  [structlog](https://www.structlog.org/) under the `django_dbos` logger.
- **A pytest plugin** — `dbos_inline` runs enqueued tasks synchronously for
  fast unit tests; `dbos_system` launches a real DBOS system database
  (SQLite or a Postgres testcontainer) when you need real semantics.

## Requirements

- Python 3.12+
- Django 6.0+
- A DBOS system database: PostgreSQL (recommended) or SQLite

## Installation

```bash
uv add django-dbos
```

Add the `msgspec` extra (`uv add "django-dbos[msgspec]"`) for fast, safe
serialization of task data; see
[Serialization](https://github.com/lewoudar/django-dbos/blob/main/docs/configuration.md#serialization).

```python
INSTALLED_APPS = [
    ...,
    'django_dbos',
]

TASKS = {
    'default': {
        'BACKEND': 'django_dbos.backends.DBOSBackend',
        'QUEUES': ['default', 'emails'],
    },
}

DJANGO_DBOS = {
    # every queue a task can use must be declared here too
    'QUEUES': {'default': None, 'emails': None},
}
```

```bash
python manage.py migrate       # django-dbos' own table (worker heartbeats)
python manage.py dbos migrate  # the DBOS system database
python manage.py dbos worker   # start executing tasks
```

Put your tasks in a `tasks.py` module of an installed app: that is where the
worker looks for them. Your web processes only ever talk to the system
database — they enqueue tasks, they never execute them.

New to background tasks? [Your first task](https://github.com/lewoudar/django-dbos/blob/main/docs/first-task.md) walks through
writing, running and checking one, step by step.

## Using an AI coding agent

django-dbos ships an [agent skill](https://github.com/lewoudar/django-dbos/tree/main/skills/django-dbos)
that teaches AI coding agents (Claude Code, Codex, Cursor, OpenCode, Pi...)
how to write tasks, workflows and cron schedules with it. Install it with the
[`skills`](https://github.com/vercel-labs/skills) CLI:

```bash
npx skills add lewoudar/django-dbos --skill django-dbos
```

[AI agents](https://github.com/lewoudar/django-dbos/blob/main/docs/ai-agents.md) has the command for each agent
and how to install the skill by hand.

## Examples

Two runnable projects in [`examples/`](https://github.com/lewoudar/django-dbos/tree/main/examples/), each a standalone `uv`
project you can `uv sync` and run on its own:

- [`ninja_api`](https://github.com/lewoudar/django-dbos/tree/main/examples/ninja_api) — a [Django Ninja](https://django-ninja.dev/)
  API that enqueues a task and polls its status until it's done.
- [`cron_report`](https://github.com/lewoudar/django-dbos/tree/main/examples/cron_report) — a `@cron` task that fires on its
  own once a worker is running, no external scheduler needed.

## Documentation

The full documentation lives in [`docs/`](https://github.com/lewoudar/django-dbos/tree/main/docs/) (a hosted version is coming
once the repository goes public). Good places to start:

- [Your first task](https://github.com/lewoudar/django-dbos/blob/main/docs/first-task.md) — a hands-on walkthrough.
- [Tasks](https://github.com/lewoudar/django-dbos/blob/main/docs/tasks.md) and [Workflows](https://github.com/lewoudar/django-dbos/blob/main/docs/workflows.md) — options,
  retries, durable primitives.
- [Cron scheduling](https://github.com/lewoudar/django-dbos/blob/main/docs/cron.md) — schedules and Sentry monitors.
- [Deployment](https://github.com/lewoudar/django-dbos/blob/main/docs/deployment.md) — what to settle before production.
- [Troubleshooting](https://github.com/lewoudar/django-dbos/blob/main/docs/troubleshooting.md) — the errors you are most
  likely to meet.

## License

MIT
