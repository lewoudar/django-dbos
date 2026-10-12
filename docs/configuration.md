# Configuration

django-dbos is configured through a single `DJANGO_DBOS` setting.

## System database

DBOS records workflow executions in a **system database**. By default,
django-dbos derives its URL from your default Django connection:

- **PostgreSQL**: same server, dedicated database `dbos_<dbname>` (created
  by `manage.py migrate`, along with DBOS' tables and migrations).
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

`DJANGO_DBOS` accepts the keys below; `manage.py check` reports any other
one (`django_dbos.E003`, see [System checks](troubleshooting.md#system-checks)).

| Key | Default | Description |
| --- | --- | --- |
| `NAME` | `DBOS_APPLICATION_NAME` env, else `"django"` | DBOS application name. |
| `APPLICATION_VERSION` | auto | Application version recorded with workflow executions. By default DBOS derives it from the workflow source code. |
| `SYSTEM_DATABASE_URL` | derived | URL of the DBOS system database (see above). |
| `SYSTEM_DB_PREFIX` | `"dbos_"` | Prefix of the derived system database name. |
| `SYSTEM_DB_SCHEMA` | `"dbos"` | Schema holding DBOS system tables (PostgreSQL). |
| `QUEUES` | `{"default": None}` | Declared queues and their options (see below). |
| `EXECUTOR_ID` | `DBOS__VMID` env, else `"local"` | Identity of a worker instance (see [Executor identity](#executor-identity)). |
| `SERIALIZER` | DBOS default (pickle) | How task arguments, results and errors are stored (see [Serialization](#serialization)). |
| `CAPACITY` | `{}` | Thresholds and advisor of the scaling advice (see [Worker capacity](status.md#thresholds)). |
| `RUN_MIGRATIONS` | `True` | Whether workers create/migrate the system database on launch (see [Migrating the system database](#migrating-the-system-database)). |
| `MIGRATE_WITH_DJANGO` | `True` | Whether `manage.py migrate` also creates/migrates the system database (see [Migrating the system database](#migrating-the-system-database)). |
| `SCHEDULER_POLLING_INTERVAL_SEC` | `30.0` | How often the scheduler thread looks for new schedules. |
| `ADMIN_ENABLED` | `True` | Register the DBOS pages in the Django admin (see [Admin](admin.md#options)). |
| `ADMIN_SITE` | `["django.contrib.admin.site"]` | Admin sites (dotted paths) to register the pages on. |

## Serialization

Task arguments, results and errors are stored in the system database with
DBOS' serializer, which is **pickle** by default. Pickle accepts any Python
object but is not safe against untrusted data: anyone able to write to the
system database can run code in your workers and in every process reading
results. Treat the system database as a trusted store, or pick a safe
serializer:

| `SERIALIZER` | Safe | Speed | Stored as | Values that come back as they were |
| --- | --- | --- | --- | --- |
| `None` (DBOS' pickle) | no | fast | base64 pickle | any Python object |
| `'django_dbos.serializers.MsgspecJSONSerializer'` | yes | fastest | JSON | JSON types |
| `'django_dbos.serializers.MsgspecMsgpackSerializer'` | yes | fast | base64 msgpack | JSON types, aware `datetime`, `bytes` |
| `'dbos.DBOSPortableJSONSerializer'` | yes | slow | JSON | JSON types |

On a 1000-row payload, a round trip takes about 0.36 ms with msgspec JSON,
0.56 ms with msgspec msgpack, 0.78 ms with pickle and 2.9 ms with DBOS'
portable JSON.

The msgspec serializers need the `msgspec` extra:

```bash
uv add "django-dbos[msgspec]"
# or
pip install "django-dbos[msgspec]"
```

```python
DJANGO_DBOS = {
    'SERIALIZER': 'django_dbos.serializers.MsgspecJSONSerializer',
}
```

With any safe serializer, task arguments and results must be plain data.
Tuples and sets come back as lists; `Decimal`, `UUID`, dates, times, enums
and naive datetimes come back as strings; dataclasses as dicts. With the
msgspec serializers, a failed task's `TaskResult.errors` still reports the
original exception class and its traceback; the class itself is never
imported when the error is read back.

`SERIALIZER` accepts a dotted path, a `dbos.Serializer` subclass or an
instance (a custom serializer implements `serialize()` / `deserialize()`).
Workers and web processes must use the same one, and switching it while
workflows are in flight makes their stored data unreadable.

## Queues

Queues process task executions concurrently and are declared under
`DJANGO_DBOS["QUEUES"]`. Queue names used by tasks (through `queue_name` or
`TASKS[alias]["QUEUES"]`) must be declared here, otherwise enqueuing raises
`QueueNotDeclared`. Each queue maps to `None` (no options) or a dict of
options:

| Option | Default | Description |
| --- | --- | --- |
| `worker_concurrency` | unlimited | Max executions of this queue running at once in one worker. |
| `global_concurrency` | unlimited | Max executions of this queue running at once across all workers. |
| `limiter` | none | Rate limit, `{'limit': n, 'period': seconds}`: at most `n` executions started per `period`. |
| `polling_interval_sec` | `1.0` | How often workers check the queue for new executions. |
| `capacity` | global thresholds | Per-queue thresholds of the scaling advice (django-dbos only, see [Worker capacity](status.md#thresholds)). |

An unknown option is a configuration error, reported by `manage.py check`.

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
| Web (gunicorn, uvicorn, `runserver`), shell, other commands | A `DBOSClient` against the system database: enqueue tasks, read results, admin pages, `dbos status`. Never executes a task. |
| `dbos worker` | The DBOS application: registers every task as a workflow, launches the queue workers, the scheduler and crash recovery, executes tasks. |

The client is created on first use and never runs schema migrations: the
system database must exist before the first enqueue — created by
`manage.py migrate`, `manage.py dbos migrate` or a worker's launch (see
[Migrating the system database](#migrating-the-system-database)).

The two sides also react differently to a lost system database, on purpose:
a worker follows DBOS' default and **waits** for the connection to come back
(pausing its workflows rather than failing them), while the client **fails
fast** so a web request never hangs on it. The admin pages then show an
error page and `dbos status` exits with an error, both with a message where
database passwords are masked.

!!! warning
    Error messages from the DBOS SDK and SQLAlchemy can quote the database
    URL. django-dbos masks the password in everything it reports, but
    Django's own `DEBUG=True` error page prints the local variables of every
    frame — including URLs held by the SDK. Keep `DEBUG=False` in
    production, as always.

## Launching workers

Tasks are executed by the `dbos worker` management command:

```bash
python manage.py dbos worker
```

The worker loads tasks from every installed app's `tasks` (or `dbos_tasks`)
module, registers them as DBOS workflows, launches DBOS' queue workers and the
cron scheduler, and shuts down gracefully on `SIGTERM`/`SIGINT`. It also
pushes a heartbeat to the Django database (see [Worker capacity](status.md)),
which is why django-dbos ships a migration.

Every worker also takes part in [cron scheduling](cron.md): run as many
workers as you like, each scheduled firing still executes once.

### Migrating the system database

`manage.py migrate` creates the system database and applies DBOS' schema
migrations once Django's own migrations are done: one command migrates
everything, wherever you already run it (release step, container entrypoint,
CI). It reads the URL and schema from `DJANGO_DBOS`, connects with a plain
database handle (no worker, no background thread), prints a credential-free
summary and is idempotent.

It only runs for `migrate` on the default database (the system database is
derived from it), not for `flush`, and never on a test database: test runs
(Django's runner, pytest-django) leave the system database alone. If the
system database is unreachable, or the migrating role may not create it,
`migrate` fails *after* applying Django's migrations; run it again once
fixed.

Prefer to keep the two apart — say the system database lives elsewhere and
needs other credentials? Turn it off and run `dbos migrate` yourself, which
also grants the schema to your runtime role:

```python
DJANGO_DBOS = {
    'MIGRATE_WITH_DJANGO': False,
}
```

```bash
python manage.py migrate
python manage.py dbos migrate            # creates the database and applies DBOS' migrations
python manage.py dbos migrate --app-role app   # optionally grant the schema to your runtime role
```

Workers migrate too: by default (`RUN_MIGRATIONS = True`), every worker
creates the system database and applies DBOS' migrations when it launches.
Handy in development. Once `migrate` does it in your release step, turn that
off in production: workers then only check that the schema is there and up
to date, and refuse to launch otherwise. Their database role no longer needs
DDL rights, and a DBOS upgrade changes the schema during the release, not
whenever the first new worker boots.

```python
DJANGO_DBOS = {
    'RUN_MIGRATIONS': False,
}
```

See [Deployment](deployment.md).

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
    'EXECUTOR_ID': os.environ.get('WORKER_INSTANCE_ID'),  # whatever names an instance on your platform
}
```

`None` falls back to the `DBOS__VMID` environment variable DBOS itself reads,
then to `"local"`. [Deployment](deployment.md) shows which variable to use on
common platforms.
