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
| `EXECUTOR_ID` | `DBOS__VMID` env, else `"local"` | Identity of a worker instance (see [Executor identity](#executor-identity)). |
| `SERIALIZER` | DBOS default (pickle) | How task arguments, results and errors are stored (see [Serialization](#serialization)). |
| `CAPACITY` | `{}` | Thresholds and advisor of the scaling advice (see [Worker capacity](status.md#thresholds)). |
| `SCHEDULER_POLLING_INTERVAL_SEC` | `30.0` | How often the scheduler thread looks for new schedules. |

## Serialization

Task arguments, results and errors are stored in the system database with
DBOS' serializer, which is **pickle** by default. Pickle accepts any Python
object but is not safe against untrusted data: anyone able to write to the
system database can run code in your workers and in every process reading
results. Treat the system database as a trusted store — or switch to
DBOS' portable JSON serializer when your task arguments and results are
plain data (JSON scalars, lists, dicts, datetimes, ...):

```python
DJANGO_DBOS = {
    'SERIALIZER': 'dbos.DBOSPortableJSONSerializer',
}
```

`SERIALIZER` accepts a dotted path, a `dbos.Serializer` subclass or an
instance (a custom serializer implements `serialize()` / `deserialize()`).
Workers and web processes must use the same one, and switching it while
workflows are in flight makes their stored data unreadable.

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
            'capacity': {'SCALE_UP_BACKLOG_AGE_SECONDS': 300},  # django-dbos only, see Worker capacity
        },
    },
}
```

## Processes

django-dbos follows DBOS' own split between an *application* and its *clients*:

| Process | What it does with DBOS |
| --- | --- |
| Web (gunicorn, uvicorn, `runserver`), shell, other commands | A `DBOSClient` against the system database: enqueue tasks, read results, admin pages, `dbos_status`. Never executes a task. |
| `dbos_worker` | The DBOS application: registers every task as a workflow, launches the queue workers, the scheduler and crash recovery, executes tasks. |

The client is created on first use and never runs schema migrations: **start a
worker once before enqueuing** — it creates the system database and its
tables on launch.

## Launching workers

Tasks are executed by the `dbos_worker` management command:

```bash
python manage.py dbos_worker
```

The worker loads tasks from every installed app's `tasks` (or `dbos_tasks`)
module, registers them as DBOS workflows, launches DBOS' queue workers and the
cron scheduler, and shuts down gracefully on `SIGTERM`/`SIGINT`. It also
pushes a heartbeat to the Django database (see [Worker capacity](status.md)),
which is why django-dbos ships a migration.

Use `--no-scheduler` to run a worker that does not manage cron schedules
(useful when running several workers behind a load balancer and you want a
dedicated scheduler).

### Executor identity

DBOS records an `executor_id` on every workflow a worker runs, and a
restarting worker recovers the interrupted workflows *of its own id*. By
default every worker is `"local"`, which works but makes them
indistinguishable in reports. Give each worker instance a **stable**
identity — stable across restarts of the same instance, distinct between
instances:

```python
import os

DJANGO_DBOS = {
    'EXECUTOR_ID': os.environ.get('FLY_MACHINE_ID'),  # fly.io; the pod name on Kubernetes, ...
}
```

`None` falls back to the `DBOS__VMID` environment variable DBOS itself reads,
then to `"local"`.
