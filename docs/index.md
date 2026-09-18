# django-dbos

**DBOS-backed backend for [Django's tasks framework](https://docs.djangoproject.com/en/stable/topics/tasks/)**:
durable background tasks, cron scheduling and optional Sentry monitoring.

```python
from django.tasks import task


@task
def send_welcome_email(user_id: int): ...


result = send_welcome_email.enqueue(user_id)  # durable, survives crashes
```

If you know [Django's tasks API](https://docs.djangoproject.com/en/stable/ref/tasks/), you already
know django-dbos: tasks are declared with `@task`, enqueued with `.enqueue()`, and results are
standard `TaskResult` objects. Under the hood, executions are [DBOS](https://docs.dbos.dev/)
workflows recorded in a system database, which makes them fault-tolerant: if a worker crashes,
tasks resume where they stopped.

## Requirements

- Python 3.12+
- Django 6.0+
- A DBOS system database: PostgreSQL (recommended) or SQLite

## Installation

```bash
uv add django-dbos
```

Add the app and register the backend in your settings:

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

Apply the migrations — Django's (django-dbos ships one small table, for
worker heartbeats) and DBOS' (the system database) — then start a worker:

```bash
python manage.py migrate
python manage.py dbos migrate
python manage.py dbos worker
```

That's it — every task enqueued through this backend is now durable. Your
web processes only talk to the DBOS system database; they never execute
tasks. See [Configuration](configuration.md) for the database setup and the
available options.

## Examples

Two runnable projects, each a standalone `uv` project you can `uv sync` and
run on its own:
[`examples/ninja_api`](https://github.com/lewoudar/django_dbos/tree/main/examples/ninja_api)
(a [Django Ninja](https://django-ninja.dev/) API that enqueues a task and
polls its status) and
[`examples/cron_report`](https://github.com/lewoudar/django_dbos/tree/main/examples/cron_report)
(a `@cron` task that fires on its own once a worker is running).

## License

MIT
