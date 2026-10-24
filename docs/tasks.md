---
icon: lucide/list-checks
---

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

`result` is a standard Django `TaskResult`. Its state lives in the system
database, so you can check on it later — from the same object or, with its
`id`, from any other process:

```python
result.refresh()  # reload the latest state
result.status  # TaskResultStatus.READY / RUNNING / SUCCESSFUL / FAILED
result.return_value  # only available when SUCCESSFUL

result = send_welcome_email.get_result(result_id)  # e.g. in another request
```

`READY` means waiting for a worker (or for its `run_after` time), `RUNNING`
that a worker is executing it. A task that was cancelled from the admin, or
that exhausted its recovery attempts, is `FAILED`.

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

A task's priority is an integer from -100 to 100, and **the larger the
number, the sooner the task runs**: a task with priority 10 is picked up
before one with priority 1, which is itself picked up before one with
priority -5. The default is 0.

```python
@task(priority=50)
def urgent_task(): ...


@task(priority=-50)
def cleanup_task(): ...


# Override the priority for a single enqueue.
cleanup_task.using(priority=80).enqueue()
```

Priorities only order the tasks **waiting in the same queue**: when a worker
has a free slot, it takes the waiting task with the highest priority, and
the oldest one among tasks with equal priority. A task that is already
running is never interrupted, and tasks in different queues don't compete
with each other.

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

`context.task_result` is the in-progress `TaskResult` for this execution, read
from the system database. `context.attempt` counts the executions of this
task, current one included: `1` on the first run, `2` when a worker picks it
up again after a crash, and so on. Step retries (`step_retries_allowed`)
happen inside one execution and are not counted. Once the task is done,
`result.attempts` reports the same number.

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
    `takes_context=True` also works with [`@cron`](cron.md). A cron task is
    called without arguments, so `@cron` refuses functions with required
    parameters, but the context doesn't count: Django injects it.

    ```python
    from django.tasks import task
    from django_dbos import cron


    @cron('0 3 * * *')
    @task(takes_context=True)
    def nightly_report(context) -> None:
        print(f'attempt {context.attempt}')
    ```

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
| `deduplication_id` | `None` | While a workflow with this ID is enqueued or running, further enqueues raise an error: a fixed string, or a callable receiving the task's arguments (see below). |
| `workflow_timeout` | `None` | Max wall-clock duration of the whole execution, in seconds, counted from the moment a worker starts it (time spent waiting in the queue or in `run_after` does not count). The task is [cancelled](#cancelling-a-task) at the deadline and its result reports `FAILED` with a `TaskTimedOut` error. |
| `max_recovery_attempts` | DBOS default | How many times DBOS recovers the task after worker crashes. Beyond that it gives up: the result reports `FAILED` with a `MaxRecoveryAttemptsExceeded` error. |
| `durable_operations` | `False` | Enable durable primitives inside the task (see below). |
| `partition_key` | `None` | On a partitioned queue, the partition of each execution: the name of a task argument, or a callable receiving the task's arguments (see below). |

!!! note "Step retries and durable operations"
    The `step_*` options retry the task function **as a whole**, which is
    what happens by default: the function runs as a single DBOS step. With
    `durable_operations=True`, the function is no longer a step, so these
    options are ignored (`django_dbos.W006`): set retries on each step
    instead, see [Retrying a step](workflows.md#retrying-a-step).

    In the default mode, a `@DBOS.step` function called by the task runs
    inside the task's own step, so DBOS executes it as a plain function:
    its own retry options don't apply either, only the `step_*` options do.

### Partition keys

On a [partitioned queue](configuration.md#partitioned-queues), each
execution belongs to a partition, and the queue's `partition_*` limits apply
per partition. `partition_key` tells django-dbos where to find it in the
task's arguments, so the calling code stays plain Django:

```python
@dbos_params(partition_key='user_id')  # the value of the user_id argument, as a string
@task(queue_name='reports')
def build_report(user_id: int, kind: str = 'monthly'): ...


build_report.enqueue(42)  # partition '42', positional or keyword alike
```

For a key that is not a single argument, pass a callable; it receives the
arguments given to `enqueue()` and its result is converted to a string:

```python
@dbos_params(partition_key=lambda user_id, kind='monthly': f'{user_id}-{kind}')
@task(queue_name='reports')
def build_report(user_id: int, kind: str = 'monthly'): ...
```

The key cannot be `None` or empty. It is ignored when the task runs on a
queue that is not partitioned (say, after `.using(queue_name='default')`),
and a task without one cannot be enqueued on a partitioned queue. The
partition of an execution is shown on its admin page.

### Deduplication

`deduplication_id` rejects an enqueue while another execution with the same
ID is still waiting or running on the queue: the enqueue raises
`DBOSQueueDeduplicatedError` (see
[Troubleshooting](troubleshooting.md#dbosqueuededuplicatederror-when-enqueuing)).

A string is a fixed ID, shared by every enqueue of the task: at most one
execution of it can be pending at any time.

```python
@dbos_params(deduplication_id='nightly-cleanup')
@task
def cleanup(): ...
```

To deduplicate per argument, pass a callable; it receives the arguments
given to `enqueue()` and its result is converted to a string:

```python
@dbos_params(deduplication_id=lambda user_id: f'sync-user-{user_id}')
@task
def sync_user(user_id: int): ...


sync_user.enqueue(42)
sync_user.enqueue(7)  # fine: another user
sync_user.enqueue(42)  # DBOSQueueDeduplicatedError while the first one is pending
```

When the callable returns `None`, that enqueue is not deduplicated: it is
never rejected and blocks no other enqueue. Use it to bypass deduplication
on demand, for example to force a sync:

```python
@dbos_params(deduplication_id=lambda user_id, force=False: None if force else f'sync-user-{user_id}')
@task
def sync_user(user_id: int, force: bool = False): ...


sync_user.enqueue(42)  # ID sync-user-42
sync_user.enqueue(42)  # DBOSQueueDeduplicatedError: sync-user-42 is still pending
sync_user.enqueue(42, force=True)  # no ID: accepted
```

!!! warning
    DBOS compares deduplication IDs per queue, regardless of the task. If
    `sync_user` and `notify_user` share a queue and both use the bare
    `user_id` as their ID, `sync_user(42)` pending makes `notify_user(42)`
    fail as a duplicate. Prefix the ID with the task's name (`sync-user-42`,
    not `42`).

Deduplication is not supported on a partitioned queue (`django_dbos.E006`).
Unlike a partition with `partition_concurrency=1`, which queues the second
execution behind the first, deduplication refuses it.

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
| `run_step(func, *args, name=..., step_options=...)` | Execute a function as a checkpointed step, optionally retried. |
| `sleep(seconds)` | Durable sleep: the workflow stops and resumes later. |
| `set_event(key, value)` | Publish an event on the current workflow. |
| `get_event(workflow_id, key)` | Wait for an event from another workflow. |
| `get_all_events(workflow_id)` | Read every event a workflow has set so far, without waiting. |
| `send(workflow_id, message, topic)` | Send a durable message to another workflow. |
| `recv(topic, timeout_seconds)` | Wait for a durable message. |
| `write_stream(key, value)` / `close_stream(key)` | Append a value to a stream of the current workflow / close it. |
| `read_stream(workflow_id, key)` / `read_stream_offset(workflow_id, key, offset)` | Read another workflow's stream, value by value / at one offset. |
| `start_child_workflow(func, *args)` | Start a child workflow without waiting for it. |
| `wait_first(handles)` | Wait for the first of several workflows to finish (see also `as_completed`). |

Every method but `start_child_workflow` and `wait_first` has an `_async`
variant (`run_step_async`, `sleep_async`, `read_stream_async`, ...) for
`async def` tasks.

See [Workflows](workflows.md) for a worked example of each primitive —
progress reporting with events, pausing for an external decision with
messages, streaming output to the browser, fanning out with child workflows.

!!! warning
    With `durable_operations=True`, the task function must be deterministic and
    is responsible for checkpointing its side effects through steps. Keep the
    default (single step) unless you need durable sleeps or multi-stage
    checkpointing.

## Cancelling a task

Django's tasks API has no cancellation, so django-dbos goes through the DBOS
client — the same call as the **Cancel** button of the [admin](admin.md):

```python
from django_dbos.client import get_client

get_client().cancel_workflow(result.id)
```

Python cannot stop a function mid-execution, so what a cancellation does
depends on where the task is:

| The task is... | Effect of the cancellation |
| --- | --- |
| waiting (in its queue, or for its `run_after` time) | It never runs. |
| running, without `durable_operations` | The function **runs to its end**, side effects included; its outcome is discarded. |
| running, with `durable_operations` | The step in progress finishes, then no further step starts. |

To make a long task stop early when cancelled, split it into steps with
[`durable_operations`](#durable-workflows): each step boundary is a point
where the cancellation takes effect.

A cancelled task's result is `FAILED`, with a
`django_dbos.exceptions.TaskCancelled` error explaining why. A task stopped
by its `workflow_timeout` is cancelled the same way, and reports
`TaskTimedOut`, a subclass of `TaskCancelled`:

```python
from django_dbos.results import is_cancelled, is_timed_out

result.refresh()
if is_timed_out(result):
    ...  # stopped by its workflow_timeout
elif is_cancelled(result):
    ...  # cancelled from the admin or the client
```

`is_cancelled` is also true for a timed-out task, as `TaskTimedOut` is a
`TaskCancelled`. `is_recovery_exhausted(result)` likewise tells a task DBOS
gave up on after [too many crashes](#options-reference). These functions
read the result as it is, without querying the system database: call
`result.refresh()` first. They return `False` for results of other backends.

`get_client().resume_workflow(result.id)` (the admin's **Resume**) undoes a
cancellation: the task continues after the last step that completed — from
the start if none did. A step that finished after the cancellation is not
run again: its result was recorded.

See [`examples/ninja_api`](https://github.com/lewoudar/django-dbos/tree/main/examples/ninja_api)
for a runnable project that enqueues a task and polls its result.
