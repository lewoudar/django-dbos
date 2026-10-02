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

## `QueueNotDeclared` when enqueuing

```
Queue 'emails' is not declared in the DJANGO_DBOS setting. Declare it under DJANGO_DBOS['QUEUES'] so a worker can process it.
```

Every queue a task can use — listed in `TASKS[alias]["QUEUES"]` or passed as
`queue_name` — must also be declared in `DJANGO_DBOS["QUEUES"]`:

```python
DJANGO_DBOS = {
    'QUEUES': {'default': None, 'emails': None},
}
```

`python manage.py check` reports undeclared queues before any enqueue
([`django_dbos.E002`](#system-checks)).

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

The ID is set on the decorator, so it is the same for every enqueue of that
task: at most one execution of it can be pending at any time.

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
you with `SILENCED_SYSTEM_CHECKS`.

- **`django_dbos.E001`** — the setting is invalid: an unknown queue option,
  a malformed `CAPACITY` threshold, a `SERIALIZER` that cannot be imported...
  The message names the faulty key, for example:

    ```
    (django_dbos.E001) DJANGO_DBOS['QUEUES']['default']: unknown option(s) concurency. Valid options: worker_concurrency, global_concurrency, limiter, polling_interval_sec, capacity.
    ```

- **`django_dbos.E002`** — a queue tasks can be enqueued on is not declared
  in `DJANGO_DBOS["QUEUES"]`; enqueuing would raise
  [`QueueNotDeclared`](#queuenotdeclared-when-enqueuing). The queues come
  from `TASKS[alias]["QUEUES"]` (`["default"]` when it is not set) or, when
  a backend accepts any queue (`"QUEUES": []`), from the tasks' `queue_name`.
- **`django_dbos.E003`** — `DJANGO_DBOS` holds a key django-dbos does not
  know, most likely a typo that would otherwise be silently ignored:

    ```
    (django_dbos.E003) DJANGO_DBOS: unknown key 'QUEUE'.
        HINT: Did you mean 'QUEUES'?
    ```

- **`django_dbos.E004`** — a `tasks.py` or `dbos_tasks.py` module cannot be
  imported. The check imports them as the worker does, so the error shows
  up in `runserver` rather than when the worker refuses to start.
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
- **`django_dbos.I001`** — your default database is neither PostgreSQL nor
  SQLite, so django-dbos cannot derive the system database URL from it. Set
  `DJANGO_DBOS["SYSTEM_DATABASE_URL"]` or the `DBOS_SYSTEM_DATABASE_URL`
  environment variable (see [System database](configuration.md#system-database)).

## `@cron` task "must not require arguments"

```
Task 'report' scheduled with @cron must not require arguments (found required parameter 'day').
```

A schedule fires the task without arguments. Give the parameter a default
value, or drop `@cron` and enqueue the task yourself with its arguments.
