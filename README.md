# django-dbos

**⚠️ Experimental project. Do not use in production.**

A [DBOS](https://docs.dbos.dev/)-backed backend for Django's
[tasks framework](https://docs.djangoproject.com/en/stable/topics/tasks/):
background tasks that survive a worker crash and pick up exactly where they
stopped, cron scheduling, optional Sentry monitoring, an admin UI to inspect
and operate on tasks, and a worker capacity report to drive autoscaling.

## Why

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

With django-dbos, one decorator and a defined step turn the loop into a
durable, checkpointed workflow:

```python
from django_dbos import dbos_params, get_dbos_context


@dbos_params(durable_operations=True)
@task
def send_newsletter(campaign_id: int) -> None:
    ctx = get_dbos_context()
    for user_id in User.objects.values_list('id', flat=True):
        ctx.run_step(send_one_email, user_id, campaign_id, name=f'email-{user_id}')
```

Each `run_step` call is recorded in the system database as it completes. Kill
the worker at #6,000, restart it anywhere, and the task resumes at #6,001 —
no duplicate sends, no external state to reconcile, no idempotency keys to
invent yourself.

`run_step` is for functions you don't control the definition of, or that need
a name computed per call (like `f'email-{user_id}'` above). If you own the
function, DBOS's own `@DBOS.step` decorator works too — call it like a
regular function and it checkpoints automatically as long as it runs inside
a `durable_operations` task:

```python
from dbos import DBOS
from django_dbos import dbos_params


@DBOS.step()
def send_one_email(user_id: int, campaign_id: int) -> None: ...


@dbos_params(durable_operations=True)
@task
def send_newsletter(campaign_id: int) -> None:
    for user_id in User.objects.values_list('id', flat=True):
        send_one_email(user_id, campaign_id)
```

You don't need any of this for a simple task — `@task` alone already gives
you a workflow that DBOS retries as a whole on crash:

```python
@task
def send_welcome_email(user_id: int) -> None: ...


result = send_welcome_email.enqueue(user_id)  # durable by default
result.status  # READY, RUNNING, SUCCESSFUL, FAILED — standard Django TaskResult
```

`durable_operations` is there for the tasks where restarting from zero is the
wrong kind of retry.

Cron schedules are just `@task`s with a firing time attached — same durability,
same retries, same admin page:

```python
from django_dbos import cron


@cron('0 3 * * *')  # every day at 3am
@dbos_params(workflow_timeout=3600, step_retries_allowed=True)
@task
def nightly_report() -> None: ...
```

Schedules are registered in DBOS itself (not in the Django process), so they
keep firing across restarts and deploys — and with the `sentry` extra
installed, each firing checks in with
[Sentry Crons](https://docs.sentry.io/product/crons/) automatically.

## What you get

- **Durable tasks** — every `.enqueue()` becomes a DBOS workflow: automatic
  retries on crash, step retries with backoff, deferred execution
  (`run_after`), deduplication, priorities and workflow timeouts, all through
  Django's own `Task` / `TaskResult` API.
- **Cron scheduling** — `@cron('0 3 * * *')` on top of `@task`. Schedules
  live in DBOS itself, so they fire across restarts and deployments, not
  only while a scheduler process happens to be running.
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
```

```bash
python manage.py migrate       # django-dbos' own table (worker heartbeats)
python manage.py dbos migrate  # the DBOS system database
python manage.py dbos worker   # start executing tasks
```

Your web processes only ever talk to the system database — they enqueue
tasks, they never execute them.

## Examples

Two runnable projects in [`examples/`](examples/), each a standalone `uv`
project you can `uv sync` and run on its own:

- [`ninja_api`](examples/ninja_api) — a [Django Ninja](https://django-ninja.dev/)
  API that enqueues a task and polls its status until it's done.
- [`cron_report`](examples/cron_report) — a `@cron` task that fires on its
  own once a worker is running, no external scheduler needed.

## Documentation

Full documentation: https://github.com/lewoudar/django_dbos/tree/main/docs
(a hosted version is coming once the repository goes public).

## License

MIT
