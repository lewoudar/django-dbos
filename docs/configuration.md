---
icon: lucide/settings
---

# Configuration

django-dbos is configured where Django expects a task backend's own
settings: the `OPTIONS` of its entry in `TASKS`.

```python
TASKS = {
    'default': {
        'BACKEND': 'django_dbos.backends.DBOSBackend',
        'OPTIONS': {
            'SERIALIZER': 'django_dbos.serializers.MsgspecJSONSerializer',
            'QUEUES': {'default': None, 'emails': {'worker_concurrency': 4}},
        },
    },
}
```

DBOS runs as a single instance per process (one system database, one
configuration), so a project has **one** DBOS backend: `manage.py check`
reports several (`django_dbos.E002`). It can sit next to other backends,
under any alias. The examples below call it `default`.

## System database

DBOS records workflow executions in a **system database**. By default,
django-dbos derives its URL from your default Django connection:

- **PostgreSQL**: same server, dedicated database `dbos_<dbname>` (created
  by `manage.py migrate`, along with DBOS' tables and migrations). The tables
  live in its `dbos` schema (`SYSTEM_DB_SCHEMA`), and `SYSTEM_DB_PREFIX`
  changes the `dbos_` prefix.
- **CockroachDB** ([django-cockroachdb](https://github.com/cockroachdb/django-cockroachdb)):
  same, see [CockroachDB](#cockroachdb) below.
- **SQLite**: a `<name>-dbos.sqlite` file next to your database. Handy for
  development and tests, not recommended in production.

Two overrides are available, by order of precedence:

1. `OPTIONS["SYSTEM_DATABASE_URL"]`
2. the `DBOS_SYSTEM_DATABASE_URL` environment variable

```python
TASKS = {
    'default': {
        'BACKEND': 'django_dbos.backends.DBOSBackend',
        'OPTIONS': {
            'SYSTEM_DATABASE_URL': 'postgresql+psycopg://user:pass@db.example.com:5432/dbos_myproject',
        },
    },
}
```

!!! note
    The URL only carries DBOS' *system* tables. Your application data stays in
    the Django databases you already have; tasks executed by the worker use
    Django's ORM as usual.

!!! tip "Keeping DBOS' tables in your Django database"
    Point `SYSTEM_DATABASE_URL` at your Django database itself: DBOS' tables
    then go to their own `dbos` schema, next to Django's tables in `public`,
    and no second database is created. Handy where creating a database is
    awkward, or when the migrating role cannot (no `CREATEDB` privilege).

### CockroachDB

DBOS supports [CockroachDB](https://docs.dbos.dev/integrations/cockroachdb)
as its system database, through the `cockroachdb` extra (SQLAlchemy's
CockroachDB dialect and the psycopg2 driver):

```bash
uv add "django-dbos[cockroachdb]"
# or
pip install "django-dbos[cockroachdb]"
```

The URL is derived from a `django_cockroachdb` default database, or set with
the `cockroachdb+psycopg2://` driver (or plain `cockroachdb://`):

```python
TASKS = {
    'default': {
        'BACKEND': 'django_dbos.backends.DBOSBackend',
        'OPTIONS': {
            'SYSTEM_DATABASE_URL': 'cockroachdb+psycopg2://user:pass@crdb.example.com:26257/dbos_myproject?sslmode=verify-full',
        },
    },
}
```

Three differences with PostgreSQL:

- **psycopg2 only.** DBOS' queries fail on CockroachDB with psycopg 3, so a
  `cockroachdb+psycopg://` URL is refused (`django_dbos.E001`).
- **The worker creates DBOS' tables.** DBOS cannot migrate CockroachDB
  outside a launched worker yet: `manage.py migrate` and `dbos migrate` only
  create the database, and each worker applies DBOS' migrations when it
  launches. Keep `RUN_MIGRATIONS` on (`django_dbos.E005` otherwise), and give
  the workers' role the rights to create tables. `dbos migrate --app-role`
  is not supported.
- **No LISTEN/NOTIFY.** CockroachDB has none: DBOS polls for notifications
  and events instead.

The SSL options of your Django database are not carried over to a derived
URL: for a secured cluster (CockroachDB Cloud), set `SYSTEM_DATABASE_URL`
with its `sslmode` and certificates.

## Options

`OPTIONS` accepts the keys below; `manage.py check` reports any other
one (`django_dbos.E003`, see [System checks](troubleshooting.md#system-checks)).

| Key | Default | Description |
| --- | --- | --- |
| `NAME` | `DBOS_APPLICATION_NAME` env, else `"django"` | DBOS application name. |
| `APPLICATION_VERSION` | `DBOS__APPVERSION` env, else computed | Version recorded with workflow executions: decides which workers recover and dequeue them. Set it explicitly, see [Application version](#application-version). |
| `SYSTEM_DATABASE_URL` | derived | URL of the DBOS system database (see above). |
| `SYSTEM_DB_PREFIX` | `"dbos_"` | Prefix of the derived system database name. |
| `SYSTEM_DB_SCHEMA` | `"dbos"` | Schema holding DBOS system tables (PostgreSQL). |
| `QUEUES` | `{"default": None}` | Declared queues and their options (see below). |
| `EXECUTOR_ID` | `DBOS__VMID` env, else `"local"` | Identity of a worker instance (see [Executor identity](#executor-identity)). |
| `SERIALIZER` | DBOS default (pickle) | How task arguments, results and errors are stored (see [Serialization](#serialization)). |
| `CAPACITY` | `{}` | Thresholds and advisor of the scaling advice (see [Worker capacity](worker-capacity.md#thresholds)). |
| `RUN_MIGRATIONS` | `True` | Whether workers create/migrate the system database on launch (see [Migrating the system database](#migrating-the-system-database)). |
| `MIGRATE_WITH_DJANGO` | `True` | Whether `manage.py migrate` also creates/migrates the system database (see [Migrating the system database](#migrating-the-system-database)). |
| `SCHEDULER_POLLING_INTERVAL_SEC` | `30.0` | How often the scheduler thread looks for new schedules, in seconds (a positive number). |
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
| `'django_dbos.serializers.MsgspecMsgpackSerializer'` | yes | fast | base64 msgpack | JSON types, aware `datetime` (normalized to UTC), `bytes` |
| `'dbos.DBOSPortableJSONSerializer'` | yes | slow | JSON | JSON types |

As an order of magnitude, measured on a developer laptop: a round trip
(serialization then deserialization) of a list of 1000 dicts, each with six
fields (ints, strings, a float, a boolean, a short list), takes about 0.5 ms
with msgspec JSON, 1 ms with msgspec msgpack, 1.5 ms with pickle and 7 ms with
DBOS' portable JSON
([`benchmarks/serializers.py`](https://github.com/lewoudar/django-dbos/blob/main/benchmarks/serializers.py)
measures them on your machine). Absolute times depend on your hardware and
payload; the ratios carry over. Next to the system database round trip every
checkpoint costs, they rarely matter: choose on safety and supported types
first.

The msgspec serializers need the `msgspec` extra:

```bash
uv add "django-dbos[msgspec]"
# or
pip install "django-dbos[msgspec]"
```

```python
TASKS = {
    'default': {
        'BACKEND': 'django_dbos.backends.DBOSBackend',
        'OPTIONS': {
            'SERIALIZER': 'django_dbos.serializers.MsgspecJSONSerializer',
        },
    },
}
```

With any safe serializer, task arguments and results must be plain data.
Tuples and sets come back as lists; `Decimal`, `UUID`, dates, times and naive
datetimes come back as strings; enums as their value (a `StrEnum` as a string,
an `IntEnum` as an int; DBOS' portable serializer rejects a plain `Enum`);
dataclasses as dicts. With the
msgspec serializers, a failed task's `TaskResult.errors` still reports the
original exception class and its traceback; the class itself is never
imported when the error is read back.

`SERIALIZER` accepts a dotted path, a `dbos.Serializer` subclass or an
instance (a custom serializer implements `serialize()` / `deserialize()`).
Workers and web processes must use the same one, and switching it while
workflows are in flight makes their stored data unreadable.

## Queues

Queues process task executions concurrently. Workers process the queues
you declare, and tasks may only target those (`queue_name`): Django raises
`InvalidTask` for any other queue. Declare them **once**, in either of two
ways:

- `QUEUES`, Django's own list of names, for queues without options:

    ```python
    TASKS = {
        'default': {
            'BACKEND': 'django_dbos.backends.DBOSBackend',
            'QUEUES': ['default', 'emails'],
        },
    }
    ```

- `OPTIONS["QUEUES"]`, a mapping of queue name to options, as soon as one
  queue needs some (see below).

Setting both is a configuration error, and so is an empty `QUEUES` list
(which Django reads as "any queue", while a worker only processes declared
queues). With neither, the only queue is `default`.

In `OPTIONS["QUEUES"]`, each queue maps to `None` (no options) or a dict of
options:

| Option | Default | Description |
| --- | --- | --- |
| `worker_concurrency` | unlimited | Max executions of this queue running at once in one worker. |
| `global_concurrency` | unlimited | Max executions of this queue running at once across all workers. |
| `limiter` | none | Rate limit, `{'limit': n, 'period': seconds}`: at most `n` executions started per `period`. |
| `polling_interval_sec` | `1.0` | How often workers check the queue for new executions. |
| `partition_concurrency` | unlimited | Max executions of one partition running at once across all workers (see [Partitioned queues](#partitioned-queues)). |
| `partition_worker_concurrency` | unlimited | Max executions of one partition running at once in one worker. |
| `partition_limiter` | none | Rate limit applied to each partition separately, `{'limit': n, 'period': seconds}`. |
| `capacity` | global thresholds | Per-queue thresholds of the scaling advice (django-dbos only, see [Worker capacity](worker-capacity.md#thresholds)). |

An unknown option is a configuration error, reported by `manage.py check`.

```python
TASKS = {
    'default': {
        'BACKEND': 'django_dbos.backends.DBOSBackend',
        'OPTIONS': {
            'QUEUES': {
                'default': None,  # no options
                'emails': {
                    'worker_concurrency': 4,  # max concurrent executions per worker
                    'global_concurrency': 10,  # max concurrent executions across workers
                    'limiter': {'limit': 50, 'period': 30},  # rate limit
                    'capacity': {'SCALE_UP_BACKLOG_AGE_SECONDS': 300},  # django-dbos only, see Worker capacity
                },
            },
        },
    },
}
```

### Partitioned queues

A partitioned queue applies its limits to each *partition* separately, for
example one execution at a time per user, however many users have work
waiting. Any `partition_*` option makes the queue partitioned; its other
options (`worker_concurrency`, `global_concurrency`, `limiter`) still apply
to the queue as a whole.

```python
TASKS = {
    'default': {
        'BACKEND': 'django_dbos.backends.DBOSBackend',
        'OPTIONS': {
            'QUEUES': {
                'default': None,
                'reports': {
                    'global_concurrency': 10,  # 10 reports at once overall...
                    'partition_concurrency': 1,  # ...but one per user
                },
            },
        },
    },
}
```

Every execution enqueued on a partitioned queue carries a partition key,
derived from the task's arguments by
[`@dbos_params(partition_key=...)`](tasks.md#partition-keys). A task
without one is refused when enqueued there. Partitioned queues come with two
limitations from DBOS: no `deduplication_id`, and no `@cron` tasks (their
executions carry no key). `manage.py check` reports all three cases
(`django_dbos.E006`).

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

## Database connections

Each process holds two sets of connections, configured separately: Django's
own, to your application databases, and DBOS', to the system database.

### Django's connections in the worker

The worker runs each task in a thread of DBOS' executor and recycles Django's
connections around it, as a request would (see
[Database connections](tasks.md#database-connections)). With Django's default
`CONN_MAX_AGE = 0`, every task that touches the ORM therefore opens a new
connection and closes it at the end: a few milliseconds per task, often more
than a short task itself, and a steady churn of connections under load. Keep
connections open instead, in one of two ways (Django refuses the
combination):

```python
# Persistent connections: one per thread running a task, reused task after task.
DATABASES['default']['CONN_MAX_AGE'] = 600

# Or Django's connection pool (psycopg 3, `psycopg[pool]`), shared by the threads.
DATABASES['default']['CONN_MAX_AGE'] = 0
DATABASES['default']['OPTIONS'] = {'pool': {'min_size': 0, 'max_size': 16, 'timeout': 30}}
```

A worker needs one connection per task running at the same time, plus one
for its heartbeat, so size a pool's `max_size` on the worker's concurrency
(the `worker_concurrency` of its queues): a task finding the pool exhausted waits up to `timeout`, then
fails.

Add `CONN_HEALTH_CHECKS = True` when something may close idle connections
behind Django's back — a proxy, a firewall, or a serverless database
suspending its compute when idle ([Neon](https://neon.com/docs/introduction/scale-to-zero), for example). Django then checks a
persistent connection before reusing it, at a small cost per task.

### DBOS' connections to the system database

DBOS keeps its own SQLAlchemy pool to the system database, used for
checkpoints, queues and events — never for your tasks' queries. With DBOS'
defaults:

| Process | Connections to the system database |
| --- | --- |
| `dbos worker` | A pool of 20, plus one dedicated connection listening for notifications (`LISTEN`). |
| Web processes and commands (`DBOSClient`) | A pool of 5, opened on first use. No listener. |

When the system database lives on the same server as your application
database (the default, see [System database](#system-database)), both sets
count towards the server's `max_connections`: plan for about 21 per worker,
5 per web process, plus Django's own connections.

### Connection poolers

A pooler such as PgBouncer sits between your processes and PostgreSQL and
shares a few server connections among many clients. How long a client keeps
a server connection depends on its mode:

- **Session mode** (PgBouncer's default): until the client disconnects. The
  client keeps a real PostgreSQL session, so everything works, but clients
  holding persistent connections each tie up a server connection, which
  saves little.
- **Transaction mode**: for one transaction only, so idle clients hold no
  server connection. That is what makes a pooler worthwhile, and the mode
  managed providers use for their pooled connection strings, such as the
  `-pooler` ones of [Neon](https://neon.com/docs/connect/connection-pooling).
  Two transactions of one client may run on different server connections,
  so whatever lives in the session is lost between them: `LISTEN`,
  `SET`, temporary tables, session advisory locks, cursors kept open
  across transactions.

The rest of this section is about transaction mode. `LISTEN` then runs
without error, but the notifications reach a connection already handed
back to the pool, and are lost.

DBOS doesn't fail over it: it re-checks the system database every 60 seconds
in case a notification was dropped. Inside a worker, `recv`,
`ctx.get_event` and `ctx.read_stream` (see [Workflows](workflows.md)) then
wake up on that re-check only, up to a minute late instead of within
milliseconds. Queues are not affected: they poll.

Every process talks to both databases, so the choice goes by database and
by process:

| | `dbos worker` | Web processes and commands |
| --- | --- | --- |
| **System database** (DBOS) | A direct connection, without a pooler: the worker listens for notifications. Its pool is long-lived and fixed in size, so a pooler would save little anyway. | No listener, so a pooler would work; but they share the worker's URL unless each process sets its own `DBOS_SYSTEM_DATABASE_URL` (with `SYSTEM_DATABASE_URL` left unset in `OPTIONS`, which wins over it). The direct URL everywhere is the simple choice. |
| **Application database** (Django) | A pooler is fine. | A pooler is fine. |

Behind a pooler in transaction mode, set `DISABLE_SERVER_SIDE_CURSORS = True`
on the application database, as
[Django suggests](https://docs.djangoproject.com/en/stable/ref/databases/#transaction-pooling-and-server-side-cursors).

A pooler doesn't make persistent connections on Django's side redundant: it
saves the database server from opening a connection per client, but each
client still connects to the pooler — a network round trip, often TLS and
authentication, a few milliseconds again for a managed pooler. Keep the
worker's connections to the pooler open with `CONN_MAX_AGE` (see [Django's
connections in the worker](#djangos-connections-in-the-worker)): they cost
the server next to nothing, as the pooler lends them a server connection for
a transaction only. Django's connection pool is then mostly redundant,
unless you need to cap the connections each process opens to the pooler:
every process holds its own pool, so the total is the number of processes
times `max_size`.

!!! warning
    A derived system database URL copies the host of your default Django
    database. If that one goes through a pooler, so does the system
    database: set `SYSTEM_DATABASE_URL` to the direct host.

A worker also polls its queues continuously: on a serverless database that
suspends its compute when idle, the compute stays up as long as a worker
runs.

## Launching workers

Tasks are executed by the `dbos worker` management command:

```bash
python manage.py dbos worker
```

The worker loads tasks from every installed app's `tasks` (or `dbos_tasks`)
module, registers them as DBOS workflows, launches DBOS' queue workers and the
cron scheduler, and shuts down gracefully on `SIGTERM`/`SIGINT`. It also
pushes a heartbeat to the Django database (see [Worker capacity](worker-capacity.md)),
which is why django-dbos ships a migration.

On shutdown, the worker waits up to `--shutdown-timeout` seconds (10 by
default) for the tasks it is running to finish, then disconnects; a task still
running is recovered later from its last checkpoint, like after a crash. Keep
it below your platform's grace period before `SIGKILL` (see
[Deployment](deployment.md)):

```bash
python manage.py dbos worker --shutdown-timeout 25
```

Every worker also takes part in [cron scheduling](cron.md): run as many
workers as you like, each scheduled firing still executes once.

### Migrating the system database

`manage.py migrate` creates the system database and applies DBOS' schema
migrations once Django's own migrations are done: one command migrates
everything, wherever you already run it (release step, container entrypoint,
CI). It reads the URL and schema from the backend's `OPTIONS`, connects with a plain
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
TASKS = {
    'default': {
        'BACKEND': 'django_dbos.backends.DBOSBackend',
        'OPTIONS': {
            'MIGRATE_WITH_DJANGO': False,
        },
    },
}
```

```bash
python manage.py migrate
python manage.py dbos migrate            # creates the database and applies DBOS' migrations
python manage.py dbos migrate --app-role app   # optionally grant the schema to your runtime role
```

On CockroachDB, both only create the database: the workers apply DBOS'
migrations (see [CockroachDB](#cockroachdb)).

Workers migrate too: by default (`RUN_MIGRATIONS = True`), every worker
creates the system database and applies DBOS' migrations when it launches.
Handy in development. Once `migrate` does it in your release step, turn that
off in production: workers then only check that the schema is there and up
to date, and refuse to launch otherwise. Their database role no longer needs
DDL rights, and a DBOS upgrade changes the schema during the release, not
whenever the first new worker boots.

```python
TASKS = {
    'default': {
        'BACKEND': 'django_dbos.backends.DBOSBackend',
        'OPTIONS': {
            'RUN_MIGRATIONS': False,
        },
    },
}
```

See [Deployment](deployment.md).

### Executor identity

DBOS records an `executor_id` on every workflow a worker runs, and a
restarting worker recovers the interrupted workflows *of its own id*. It
cannot tell an interrupted workflow from one still running: it takes back
every pending workflow of its id. By default every worker is `"local"`,
which is fine for a single worker. With several, a worker that starts
re-runs the workflows still running on the others, and the step in
progress then executes twice. Give each worker instance a **stable**
identity — stable across restarts of the same instance, so a restarted
worker recovers its own workflows, and distinct between instances, so it
recovers only those:

```python
import os

TASKS = {
    'default': {
        'BACKEND': 'django_dbos.backends.DBOSBackend',
        'OPTIONS': {
            'EXECUTOR_ID': os.environ.get('WORKER_INSTANCE_ID'),  # whatever names an instance on your platform
        },
    },
}
```

`None` falls back to the `DBOS__VMID` environment variable DBOS itself reads,
then to `"local"`. [Deployment](deployment.md) shows which variable to use on
common platforms.

### Application version

DBOS records an application version on every workflow execution, and uses
it to decide who may run it:

- **Recovery**: a restarting worker recovers the interrupted workflows of
  its own executor ID *and* its own version only.
- **Queues**: tasks enqueued from web processes carry no version; only the
  workers running the **latest** version — the one registered by the most
  recent worker launch — dequeue them.

`OPTIONS["APPLICATION_VERSION"]` sets it, then the `DBOS__APPVERSION`
environment variable. Without either, DBOS computes one: a hash of the
source code of the registered workflows, of the DBOS version and of the
application name. With django-dbos, those workflows are the generic wrappers
running your tasks, not your task functions, so the computed version:

- does **not** change when you edit a task: a new worker recovers workflows
  interrupted under the old code, and replays a durable task's checkpoints
  with the new one;
- **does** change when you add or remove a task or a `@cron`, or upgrade
  DBOS: workflows left interrupted under the old version are no longer
  recovered by the new workers.

Set the version explicitly, with one of two policies:

1. **A stable version, bumped on incompatible changes** to a durable task
   (steps added, removed or reordered, a changed signature). Ordinary
   deployments keep the version, so recovery carries on across them; bumping
   it when needed is up to you. Tasks running as a single step (the default)
   restart from the beginning when recovered and rarely need a bump: this is
   the simple choice for most projects.
2. **One version per release**, such as the commit SHA. Code never replays
   checkpoints recorded by another version, but a workflow interrupted under
   one release is not recovered by the workers of the next: let the old
   workers finish their workflows before stopping them (blue/green
   deployments), or resume them by hand.

```python
import os

TASKS = {
    'default': {
        'BACKEND': 'django_dbos.backends.DBOSBackend',
        'OPTIONS': {
            'APPLICATION_VERSION': '3',  # policy 1; or os.environ['GIT_SHA'] for policy 2
        },
    },
}
```

The version of each execution is shown on its admin page.

## Logging

django-dbos logs through [structlog](https://www.structlog.org/) under the
`django_dbos` logger: tasks enqueued, cron schedules created or deleted,
actions taken from the admin, heartbeat failures... It configures nothing
itself, so its records follow your structlog configuration. Without one,
structlog prints every level, `DEBUG` included, to stdout in a readable,
colored format. That is fine in development, but in production you usually
want levels and output to be controlled from `LOGGING`.

DBOS logs through the standard library logger `dbos`. When a worker launches,
DBOS sets that logger's level to `INFO`. If the logger has no handler yet,
DBOS also adds its own console handler and turns propagation off, so its
records bypass your `LOGGING` handlers. To keep DBOS' records in your
handlers, give the `dbos` logger a handler in `LOGGING`, as below. Web
processes never launch DBOS and leave the logger alone.

### Recommended setup

Route structlog through the standard library: django-dbos, DBOS, Django and
your own code then share the same handlers, levels and format, all declared
in `LOGGING`. This setup renders logs for the console in development and as
JSON lines in production:

```python
# settings.py
import structlog

shared_processors = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt='iso'),
]

structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        *shared_processors,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

if DEBUG:
    renderers = [structlog.dev.ConsoleRenderer()]
else:
    renderers = [structlog.processors.format_exc_info, structlog.processors.JSONRenderer()]

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'structlog': {
            '()': structlog.stdlib.ProcessorFormatter,
            'processors': [structlog.stdlib.ProcessorFormatter.remove_processors_meta, *renderers],
            # Applied to the records of standard library loggers (Django, DBOS).
            'foreign_pre_chain': shared_processors,
        },
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'structlog'},
    },
    'root': {'handlers': ['console'], 'level': 'INFO'},
    'loggers': {
        # DEBUG also logs every enqueue and every task/cron registration.
        'django_dbos': {'level': 'INFO'},
        # A handler of its own keeps DBOS from installing its console handler.
        'dbos': {'handlers': ['console'], 'propagate': False},
    },
}
```

The `django_dbos` level is then set in `LOGGING` like any other logger.
The `dbos` level is not: DBOS resets it to `INFO` when a worker launches.

`merge_contextvars` adds the variables bound with
`structlog.contextvars.bind_contextvars` to every record. If a middleware
binds a request ID that way, the `enqueued task` records of that request
carry it.

### django-structlog

[django-structlog](https://django-structlog.readthedocs.io/) documents the
same pattern (`structlog.configure` with `wrap_for_formatter`, a
`ProcessorFormatter` in `LOGGING`). With it, the `django_dbos` records go
through your django-structlog configuration and pick up the `request_id` its
middleware binds. As above, add the `dbos` logger with a handler of its own.
