---
icon: lucide/life-buoy
---

# Troubleshooting

The problems you are most likely to hit, what they look like, and how to fix
them.

## A task stays `READY` forever

The task was recorded in the system database, but no worker executed it.

- **No worker is running.** Start one: `python manage.py dbos worker`.
  Tasks enqueued while no worker runs are not lost; they start as soon as
  one does.
- **The worker does not know the task.** A worker can only run the tasks it
  imported when it started. Check that:
    - the task is defined in a module named `tasks.py` (or `dbos_tasks.py`)
      of an app listed in `INSTALLED_APPS` — the worker imports those
      modules and nothing else;
    - the worker was **restarted after the task was added**: it does not
      reload code on its own. Once restarted, it picks up the waiting
      executions.

`python manage.py dbos status` shows the backlog of every queue, which tells
you at a glance whether executions pile up. `python manage.py check` reports
tasks defined outside the modules the worker imports
([`django_dbos.W001`](#system-checks)).

## `Queue '...' is not valid for backend`

```
django.tasks.exceptions.InvalidTask: Queue 'emails' is not valid for backend.
```

Django raises it when a task names a `queue_name` the DBOS backend does not
declare, as soon as the task is defined (or `using()` is called). Workers
only process declared queues, so declare it, in `QUEUES` or in
`OPTIONS["QUEUES"]` (see [Queues](configuration.md#queues)):

```python
TASKS = {
    'default': {
        'BACKEND': 'django_dbos.backends.DBOSBackend',
        'OPTIONS': {
            'QUEUES': {'default': None, 'emails': None},
        },
    },
}
```

A queue declaration that is itself invalid (both `QUEUES` and
`OPTIONS["QUEUES"]`, an empty `QUEUES` list...) is reported by
`python manage.py check` ([`django_dbos.E001`](#system-checks)) and raises
`ImproperlyConfiguredDbos` on enqueue.

## A database error on the first enqueue

With SQLite:

```
OperationalError: (sqlite3.OperationalError) no such table: main.workflow_status
```

With PostgreSQL, an error saying the system database or its `dbos` schema
does not exist.

Web processes never create the system database: it is created by
`python manage.py migrate` (unless `MIGRATE_WITH_DJANGO` is `False`),
`python manage.py dbos migrate`, or the first worker start (unless
`RUN_MIGRATIONS` is `False`). Run one of them before enqueuing. See
[Migrating the system database](configuration.md#migrating-the-system-database).

## `migrate` fails on the DBOS system database

`python manage.py migrate` applies Django's migrations first, then DBOS':
when it ends with `DBOS migrations failed`, Django's migrations are already
applied, and running `migrate` again once the cause is fixed finishes the job.
The DBOS SDK prints the actual error just above.

- **`database "dbos_<name>" does not exist`** while migrating: the system
  database could not be *created*, most often because the migrating role
  lacks `CREATEDB` (DBOS only logs that failure and carries on, so the
  missing database is what you get to see). Grant `CREATEDB` to that role,
  create the database yourself, or point `SYSTEM_DATABASE_URL` at an
  existing database (DBOS' tables live in their own `dbos` schema).
- **`connection timeout expired`**: the system database server is
  unreachable. Connection attempts give up after 10 seconds (set your own
  `connect_timeout` in `SYSTEM_DATABASE_URL` to change it).
- The system database must not be migrated from this step — say it needs
  other credentials? Set `MIGRATE_WITH_DJANGO` to `False` and run
  `python manage.py dbos migrate` where it can.

## `NoActiveDbosContext`

`get_dbos_context()` only works while a task declared with
`@dbos_params(durable_operations=True)` is running. The two usual causes:

- **`durable_operations=True` is missing** on the task. Without it, the task
  body runs as a single step, and durable primitives such as `run_step` or
  `sleep` cannot be used inside a step.
- **It is called from inside a step** — a function passed to `run_step()` or
  decorated with `@DBOS.step`. Call `get_dbos_context()` in the task body
  itself.

See [Durable workflows](tasks.md#durable-workflows).

## `No DBOS was created yet`

Something called `DBOS.get_event()`, `DBOS.send()` or another `DBOS.*` method
outside a worker, typically in a view. Those methods need the DBOS instance,
which only exists in the `dbos worker` process. From web code, go through
django-dbos' client instead:

```python
from django_dbos.client import get_client

get_client().get_event(workflow_id, 'stage', timeout_seconds=1)
get_client().send(workflow_id, True, topic='refund-approval')
```

See [Workflows](workflows.md#progress-events-set_event-get_event).

## `DBOSQueueDeduplicatedError` when enqueuing

```
Workflow ... was deduplicated due to an existing workflow in queue default with deduplication ID nightly-cleanup.
```

The task has a `deduplication_id` (see [Task options](tasks.md#task-options))
and an execution with that ID is still waiting or running. That is the point
of deduplication; catch the error if a duplicate enqueue is expected:

```python
from dbos import error as dbos_error

try:
    cleanup.enqueue()
except dbos_error.DBOSQueueDeduplicatedError:
    pass  # already scheduled
```

A fixed ID is the same for every enqueue of that task: at most one execution
of it can be pending at any time. To only reject duplicates of the same
arguments, compute the ID with a callable (see
[Deduplication](tasks.md#deduplication)).

## A task is `FAILED` with `TaskCancelled` or `TaskTimedOut`

The task did not fail on its own: it was cancelled.

- **`TaskTimedOut`** — it ran longer than its `workflow_timeout` (see
  [Task options](tasks.md#options-reference)). Raise the timeout, or split
  the work into several tasks.
- **`TaskCancelled`** — someone cancelled it, from the admin or with
  `get_client().cancel_workflow()`.

Either way, `get_client().resume_workflow(result.id)` (or **Resume** in the
admin) picks it up again. See [Cancelling a task](tasks.md#cancelling-a-task).

## A task is `FAILED` with `MaxRecoveryAttemptsExceeded`

The task did not fail on its own either: the workers executing it were
interrupted (crash, OOM, killed during a deploy) more times than its
`max_recovery_attempts` allows, and DBOS stopped recovering it so that it
cannot crash workers forever. `dbos status` counts these tasks
("exhausted their recovery attempts").

Find out why the workers died — often the task itself, using too much
memory or time — then relaunch it with `get_client().resume_workflow(result.id)`
(or **Resume** in the admin).

## A cancelled task kept running

Expected: cancelling cannot interrupt Python code. A task without
`durable_operations` runs its function to the end, side effects included,
and only its outcome is discarded; a `durable_operations` task finishes its
current step, then stops. To make a long task stop early, split it into
steps — see [Cancelling a task](tasks.md#cancelling-a-task).

## `TaskResultDoesNotExist`

`task.get_result(result_id)` was given an ID that the system database does
not know — a typo, an ID from another environment, or an execution deleted
from the admin. Handle it like a missing object (for example with a 404).

## System checks

Django's system checks — `manage.py check`, also run by `runserver` and
`migrate` — catch most of the problems above before anything runs. Errors
make `check` fail and stop `runserver`; warnings are only displayed, unless
you pass `--fail-level WARNING`. Silence a warning that does not apply to
you with Django's
[`SILENCED_SYSTEM_CHECKS`](https://docs.djangoproject.com/en/stable/ref/settings/#silenced-system-checks)
setting.

- **`django_dbos.E001`** — the backend's configuration is invalid: an unknown queue option,
  queues declared both in `QUEUES` and in `OPTIONS["QUEUES"]`, a malformed `CAPACITY` threshold, a `SERIALIZER` that cannot be imported...
  The message names the faulty key, for example:

    ```
    (django_dbos.E001) TASKS['default']['OPTIONS']['QUEUES']['default']: unknown option(s) concurency. Valid options: worker_concurrency, global_concurrency, limiter, polling_interval_sec, partition_concurrency, partition_worker_concurrency, partition_limiter, capacity.
    ```

- **`django_dbos.E002`** — several `TASKS` entries use the DBOS backend.
  DBOS runs as one instance per process, with one system database and one
  configuration: django-dbos only reads the first entry. Keep a single DBOS
  backend and declare all its queues there.
- **`django_dbos.E003`** — the backend's `OPTIONS` hold a key django-dbos
  does not know, most likely a typo that would otherwise be silently ignored:

    ```
    (django_dbos.E003) TASKS['default']['OPTIONS']: unknown key 'QUEUE'.
        HINT: Did you mean 'QUEUES'?
    ```

- **`django_dbos.E004`** — a `tasks.py` or `dbos_tasks.py` module cannot be
  imported. The check imports them as the worker does, so the error shows
  up in `runserver` rather than when the worker refuses to start.
- **`django_dbos.E005`** — `RUN_MIGRATIONS` is `False` with a CockroachDB
  system database. There, only the workers can apply DBOS' migrations
  ([CockroachDB](configuration.md#cockroachdb)): remove the option.
- **`django_dbos.E006`** — a task targets a
  [partitioned queue](configuration.md#partitioned-queues) but has no
  `partition_key` (its enqueues are refused), has a `deduplication_id`
  (DBOS does not support it there) or is scheduled with `@cron` (its
  executions carry no key and never run). Like W001, it only sees the tasks
  the checking process has imported.
- **`django_dbos.W001`** — a task is defined in a module the worker does not
  import, so [it stays `READY` forever](#a-task-stays-ready-forever). Move
  it into (or import it from) the `tasks.py` or `dbos_tasks.py` module of an
  installed app. The check only sees the tasks the checking process has
  imported (through your URLs and views, typically): it cannot catch every
  case.
- **`django_dbos.W002`** — the system database is an in-memory SQLite
  database, usually derived from an in-memory Django database. Every
  process gets its own, so the worker never sees what the web process
  enqueues. Use a file-based database or set `SYSTEM_DATABASE_URL`.
- **`django_dbos.W003`** (`--deploy` only) — no `SERIALIZER` is set, so task
  data is stored with pickle. Switch to a safe serializer, for example
  `django_dbos.serializers.MsgspecJSONSerializer`; see
  [Serialization](configuration.md#serialization).
- **`django_dbos.W004`** (`--deploy` only) — no `EXECUTOR_ID` (nor
  `DBOS__VMID`) is set, so every worker is `"local"`. See
  [Executor identity](configuration.md#executor-identity).
- **`django_dbos.W005`** — `django_dbos` is in `INSTALLED_APPS` but no
  `TASKS` entry uses `django_dbos.backends.DBOSBackend`: tasks run on
  another backend and the django-dbos options are never read.
- **`django_dbos.W006`** — a task sets `step_*` options in `@dbos_params`
  together with `durable_operations=True`. Its function no longer runs as a
  single step, so the options are ignored: set retries on each step instead
  (see [Retrying a step](workflows.md#retrying-a-step)). Like W001, it only
  sees the tasks imported so far.

## `@cron` task "must not require arguments"

```
Task 'report' scheduled with @cron must not require arguments (found required parameter 'day').
```

A schedule fires the task without arguments. Give the parameter a default
value, or drop `@cron` and enqueue the task yourself with its arguments.
