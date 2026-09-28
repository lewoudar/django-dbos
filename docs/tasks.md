# Tasks

django-dbos implements Django's [tasks framework](https://docs.djangoproject.com/en/stable/ref/tasks/):
tasks are plain module-level functions decorated with `@task`.

```python
from django.tasks import task


@task
def send_welcome_email(user_id: int) -> None: ...
```

Enqueue it anywhere (views, forms, other tasks...):

```python
result = send_welcome_email.enqueue(user_id)
# or, for async views:
result = await send_welcome_email.aenqueue(user_id)
```

`result` is a standard Django `TaskResult`; with `supports_get_result` backends
you can poll it from anywhere:

```python
result.refresh()
result.status  # TaskResultStatus.READY / RUNNING / SUCCESSFUL / FAILED
result.return_value  # only available when SUCCESSFUL
```

## Database connections

Task functions use the ORM as usual. They run in the worker's long-lived
threads, outside any request cycle, so django-dbos recycles Django's
connections around each execution the way a request would
(`close_old_connections()` before and after): `CONN_MAX_AGE` is honoured and
a connection dropped by the database is never reused by the next task.

## Deferred tasks

Use `.using(run_after=...)` to run a task later (an aware `datetime`, as
required by Django):

```python
from datetime import timedelta

from django.utils import timezone

send_report.using(run_after=timezone.now() + timedelta(hours=1)).enqueue()
```

## Priorities

Django priorities (-100 to 100, higher first) map onto DBOS queue priorities.

```python
@task(priority=50)
def urgent_task(): ...
```

## Task context

Django's `@task(takes_context=True)` prepends a `TaskContext` as the task
function's first argument, carrying metadata about the current execution:

```python
from django.tasks import task


@task(takes_context=True)
def send_welcome_email(context, user_id: int) -> None:
    if context.attempt > 1:
        logger.warning('retrying welcome email', user_id=user_id, attempt=context.attempt)
    ...
```

`context.task_result` is the in-progress `TaskResult` for this execution;
`context.attempt` is a shortcut for the number of attempts made so far.

This is orthogonal to `@dbos_params(durable_operations=True)` (see
[Durable workflows](#durable-workflows) below): `takes_context` only changes
the function's first argument, so the two combine freely — Django's own
attempt/result metadata stays available alongside DBOS' durable primitives:

```python
from django_dbos import dbos_params, get_dbos_context


@dbos_params(durable_operations=True)
@task(takes_context=True)
def onboard_user(context, user_id: int) -> None:
    ctx = get_dbos_context()
    ctx.set_event('attempt', context.attempt)
    ctx.run_step(send_welcome_email, user_id)
```

!!! note
    `@cron` tasks skip the injected context argument when validating that the
    function takes no required parameters, so `takes_context=True` combines
    with `@cron` too.

## Task options

DBOS-specific options are attached with `@dbos_params`:

```python
from django_dbos import dbos_params


@dbos_params(
    step_retries_allowed=True,  # retry the task function on failure
    step_max_attempts=5,
    step_backoff_rate=2.0,
    deduplication_id='nightly-cleanup',  # reject duplicate enqueues
    workflow_timeout=3600,  # seconds
    max_recovery_attempts=10,  # DBOS recovery after worker crashes
)
@task
def cleanup(): ...
```

!!! tip
    `@dbos_params` works whether it is above or below `@task`, and is inert if
    the task ends up on a non-DBOS backend (handy in tests).

### Options reference

| Option | Default | Description |
| --- | --- | --- |
| `step_retries_allowed` | `False` | Retry the task function on failure. |
| `step_max_attempts` | `3` | Max attempts when step retries are enabled. |
| `step_interval_seconds` | `1.0` | Delay between step retries. |
| `step_backoff_rate` | `2.0` | Multiplier applied to the interval after each failure. |
| `deduplication_id` | `None` | While a workflow with this ID is enqueued or running, further enqueues raise an error. |
| `queue_partition_key` | `None` | Partition key for partitioned queues. |
| `workflow_timeout` | `None` | Max wall-clock duration of the whole execution, in seconds, counted from the moment a worker starts it (time spent waiting in the queue or in `run_after` does not count). The task is cancelled at the deadline and its result reports `FAILED`. |
| `max_recovery_attempts` | DBOS default | Recovery attempts after a worker crash. |
| `durable_operations` | `False` | Enable durable primitives inside the task (see below). |

## Durable workflows

By default, a task's function runs as a single checkpointed DBOS step: if the
worker crashes mid-execution, the task restarts from the beginning. For
long-running, multi-stage tasks you can opt into DBOS' durable primitives with
`durable_operations=True`: the function then runs at workflow level and calls
`get_dbos_context()` to checkpoint its work explicitly.

```python
from django_dbos import dbos_params, get_dbos_context


@dbos_params(durable_operations=True)
@task
def onboard_user(user_id: int) -> None:
    ctx = get_dbos_context()

    ctx.run_step(send_welcome_email, user_id)  # checkpointed
    ctx.sleep(86400)  # one day, durably: the worker can crash, it resumes
    ctx.run_step(send_followup_email, user_id)
```

Available on the context:

| Method | Description |
| --- | --- |
| `run_step(func, *args, name=...)` | Execute a function as a checkpointed step. |
| `run_step_async(...)` | Async variant. |
| `sleep(seconds)` / `sleep_async(seconds)` | Durable sleep: the workflow stops and resumes later. |
| `set_event(key, value)` | Publish an event on the current workflow. |
| `get_event(workflow_id, key)` | Wait for an event from another workflow. |
| `send(workflow_id, message, topic)` | Send a durable message to another workflow. |
| `recv(topic, timeout_seconds)` | Wait for a durable message. |
| `start_child_workflow(func, *args)` | Start a child workflow without waiting for it. |

See [Workflows](workflows.md) for a worked example of each primitive —
progress reporting with events, pausing for an external decision with
messages, fanning out with child workflows.

!!! warning
    With `durable_operations=True`, the task function must be deterministic and
    is responsible for checkpointing its side effects through steps. Keep the
    default (single step) unless you need durable sleeps or multi-stage
    checkpointing.

See [`examples/ninja_api`](https://github.com/lewoudar/django_dbos/tree/main/examples/ninja_api)
for a runnable project that enqueues a task and polls its result.
