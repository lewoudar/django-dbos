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
you at a glance whether executions pile up.

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

## A database error on the first enqueue

With SQLite:

```
OperationalError: (sqlite3.OperationalError) no such table: main.workflow_status
```

With PostgreSQL, an error saying the system database or its `dbos` schema
does not exist.

Web processes never create the system database: it is created by
`python manage.py dbos migrate`, or by the first worker start (unless
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

## System check errors

Django's system checks — `manage.py check`, also run by `runserver` and
`migrate` — validate the `DJANGO_DBOS` setting:

- **`django_dbos.E001`** — the setting is invalid: an unknown queue option,
  a malformed `CAPACITY` threshold, a `SERIALIZER` that cannot be imported...
  The message names the faulty key, for example:

    ```
    (django_dbos.E001) DJANGO_DBOS['QUEUES']['default']: unknown option(s) concurency. Valid options: worker_concurrency, global_concurrency, limiter, polling_interval_sec, capacity.
    ```

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
