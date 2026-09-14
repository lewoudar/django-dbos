# Configuration

django-dbos is configured through a single `DJANGO_DBOS` setting.

## System database

DBOS records workflow executions in a **system database**. By default,
django-dbos derives its URL from your default Django connection:

- **PostgreSQL**: same server, dedicated database `dbos_<dbname>` (created
  automatically on first launch, along with DBOS' tables and migrations).
- **SQLite**: a `<name>-dbos.sqlite` file next to your database. Handy for
  development and tests, not recommended in production.

Two overrides are available, by order of precedence:

1. `DJANGO_DBOS["SYSTEM_DATABASE_URL"]`
2. the `DBOS_SYSTEM_DATABASE_URL` environment variable

```python
DJANGO_DBOS = {
    'SYSTEM_DATABASE_URL': 'postgresql+psycopg://user:pass@db.example.com:5432/dbos_myproject',
}
```

!!! note
    The URL only carries DBOS' *system* tables. Your application data stays in
    the Django databases you already have; tasks executed by the worker use
    Django's ORM as usual.

## Options

| Key | Default | Description |
| --- | --- | --- |
| `NAME` | `"django"` | DBOS application name. |
| `APPLICATION_VERSION` | auto | Application version recorded with workflow executions. By default DBOS derives it from the workflow source code. |
| `SYSTEM_DATABASE_URL` | derived | URL of the DBOS system database (see above). |
| `SYSTEM_DB_PREFIX` | `"dbos_"` | Prefix of the derived system database name. |
| `SYSTEM_DB_SCHEMA` | `"dbos"` | Schema holding DBOS system tables (PostgreSQL). |
| `QUEUES` | `{"default": None}` | Declared queues and their options (see below). |
| `AUTO_INIT` | `True` | Initialize DBOS on app ready (disable to control it yourself). |
| `SCHEDULER_POLLING_INTERVAL_SEC` | `30.0` | How often the scheduler thread looks for new schedules. |

## Queues

Queues process task executions concurrently and are declared under
`DJANGO_DBOS["QUEUES"]`. Queue names used by tasks (through `queue_name` or
`TASKS[alias]["QUEUES"]`) must be declared here.

```python
DJANGO_DBOS = {
    'QUEUES': {
        'default': None,  # no options
        'emails': {
            'worker_concurrency': 4,  # max concurrent executions per worker
            'global_concurrency': 10,  # max concurrent executions across workers
            'limiter': {'limit': 50, 'period': 30},  # rate limit
        },
    },
}
```

## Launching workers

Tasks are executed by the `dbos_worker` management command:

```bash
python manage.py dbos_worker
```

The worker loads tasks from every installed app's `tasks` (or `dbos_tasks`)
module, launches DBOS' queue workers and the cron scheduler, and shuts down
gracefully on `SIGTERM`/`SIGINT`.

Use `--no-scheduler` to run a worker that does not manage cron schedules
(useful when running several workers behind a load balancer and you want a
dedicated scheduler).
