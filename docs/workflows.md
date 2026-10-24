---
icon: lucide/workflow
---

# Workflows

A task with `@dbos_params(durable_operations=True)` runs its function at
*workflow level*: instead of executing as a single checkpointed step, the
function itself calls [`get_dbos_context()`](tasks.md#durable-workflows) to
checkpoint its own operations, one durable primitive at a time. This page
works through each primitive with a realistic example.

!!! warning
    A `durable_operations` function must be deterministic and re-runnable:
    DBOS replays it from the top after a crash, skipping the primitives
    already checkpointed, until it reaches the point where it stopped. Keep
    non-deterministic or side-effecting code (network calls, random values,
    `timezone.now()`) inside `run_step`/`@DBOS.step`, never directly in the
    function body.

`durable_operations` is independent of Django's own `takes_context=True` (see
[Task context](tasks.md#task-context)) — a task can read `context.attempt`
and call `get_dbos_context()` in the same function.

Every primitive below but those of child workflows (`start_child_workflow`,
`wait_first`, `as_completed`) has an `_async` variant (`sleep_async`,
`set_event_async`, `read_stream_async`, ...) to `await` from an `async def`
task.

## Steps: `run_step` / `run_step_async`

The bread and butter of a durable workflow: run a function once, checkpoint
its result, never repeat it on replay.

```python
from django_dbos import dbos_params, get_dbos_context


@dbos_params(durable_operations=True)
@task
def onboard_user(user_id: int) -> None:
    ctx = get_dbos_context()
    ctx.run_step(send_welcome_email, user_id)
```

`ctx.run_step(func, *args, name=...)` is for functions you don't control the
definition of, or that need a name computed per call. If you own the
function, DBOS's own `@DBOS.step` decorator works too — call it directly with
its normal arguments, no context needed:

```python
from dbos import DBOS


@DBOS.step()
def send_welcome_email(user_id: int) -> None: ...


@dbos_params(durable_operations=True)
@task
def onboard_user(user_id: int) -> None:
    send_welcome_email(user_id)  # checkpointed: this task runs at workflow level
```

Both styles checkpoint the same way and can be mixed freely.

### Retrying a step

A step is not retried by default: if it raises, the task fails. The
`step_*` options of `@dbos_params` don't help here, they only apply to a
task running as a single step, so configure retries on each step instead.

With `run_step`, pass DBOS's step options in `step_options`; the other
keyword arguments still go to the function:

```python
@dbos_params(durable_operations=True)
@task
def onboard_user(user_id: int) -> None:
    ctx = get_dbos_context()
    ctx.run_step(
        send_welcome_email,
        user_id,
        step_options={'retries_allowed': True, 'max_attempts': 5, 'interval_seconds': 2.0, 'backoff_rate': 2.0},
    )
```

With `@DBOS.step`, pass the same options to the decorator:

```python
@DBOS.step(retries_allowed=True, max_attempts=5)
def send_welcome_email(user_id: int) -> None: ...
```

| Option | DBOS default | Description |
| --- | --- | --- |
| `retries_allowed` | `False` | Retry the step when it raises. |
| `max_attempts` | `3` | Max attempts, the first one included. |
| `interval_seconds` | `1.0` | Delay before the first retry. |
| `backoff_rate` | `2.0` | Multiplier applied to the delay after each failed attempt. |

`step_options` can also carry the step's `name`; the `name=` argument takes
precedence over it.

## Durable sleep: `sleep` / `sleep_async`

The workflow stops entirely — no thread, no worker slot held — and resumes
after the delay, even across a worker restart:

```python
@dbos_params(durable_operations=True)
@task
def onboard_user(user_id: int) -> None:
    ctx = get_dbos_context()
    ctx.run_step(send_welcome_email, user_id)
    ctx.sleep(86400)  # one day, durably
    ctx.run_step(send_followup_email, user_id)
```

## Progress events: `set_event` / `get_event`

`set_event` publishes a key/value pair on the running workflow; anything that
knows the workflow ID can read it back with `get_event` — including code that
isn't itself a workflow, like a Django view.

```python
@dbos_params(durable_operations=True)
@task
def generate_report(report_id: int) -> str:
    ctx = get_dbos_context()
    ctx.set_event('stage', 'collecting-rows')
    rows = ctx.run_step(collect_rows, report_id)

    ctx.set_event('stage', 'rendering-pdf')
    path = ctx.run_step(render_pdf, rows)

    ctx.set_event('stage', 'done')
    return path
```

A key holds a single value: each `set_event` on it replaces the previous
one, so a reader always gets the latest — here, the stage the report has
reached when it asks.

A view polling that same workflow (`result.id` from `.enqueue()`, or the
workflow ID shown in the admin) reads the event through django-dbos' client,
the same `DBOSClient` the backend uses to enqueue tasks:

```python
from django_dbos.client import get_client


def report_progress(request, workflow_id):
    stage = get_client().get_event(workflow_id, 'stage', timeout_seconds=1)
    return JsonResponse({'stage': stage})
```

!!! warning
    Don't call `DBOS.get_event` / `DBOS.send` from a view: those need the
    `DBOS` instance, which only exists in the `dbos worker` process (see
    [Processes](configuration.md#processes)). In a web process they raise
    `No DBOS was created yet`.

`get_event` returns at once when the key has a value. Before the workflow
sets it for the first time, it waits up to `timeout_seconds` and returns
`None` rather than raising: the view reports `{"stage": null}`, the report
hasn't started yet.

Inside a workflow, `ctx.get_all_events(workflow_id)` reads every event a
workflow has set so far, as a dict, without waiting. The client has no
equivalent: a view reads events one key at a time with `get_event`.

## Messages: `send` / `recv`

Where events are a value anyone can read, messages are point-to-point and
consumed once: `recv` blocks the workflow until a matching `send` arrives (or
times out). Typical use: pause a workflow for a human decision.

```python
@dbos_params(durable_operations=True)
@task
def process_refund(order_id: int) -> str:
    ctx = get_dbos_context()
    ctx.run_step(flag_for_review, order_id)

    approved = ctx.recv(topic='refund-approval', timeout_seconds=3 * 86400)
    if not approved:
        return 'expired'

    ctx.run_step(issue_refund, order_id)
    return 'refunded'
```

The approval itself comes from outside the workflow — an admin action, say —
using the *task's* workflow ID (`result.id`) and the client, no workflow
context required:

```python
from django_dbos.client import get_client


def approve_refund(request, workflow_id):
    get_client().send(workflow_id, True, topic='refund-approval')
```

`recv` (unlike `get_event`) must be called from inside a running workflow —
it has no meaning otherwise.

## Streams: `write_stream` / `close_stream` / `read_stream`

An event holds one value, overwritten by the next `set_event`; a stream is
an ordered, append-only sequence of values. The workflow appends to it with
`write_stream` and closes it with `close_stream`; readers get every value, in
order, as it is written. Typical use: send a task's output to the browser
while it is still running.

```python
@dbos_params(durable_operations=True)
@task
def export_orders(customer_id: int) -> int:
    ctx = get_dbos_context()
    order_ids = ctx.run_step(list_order_ids, customer_id)
    for order_id in order_ids:
        line = ctx.run_step(render_order_line, order_id)
        ctx.write_stream('lines', line)
    ctx.close_stream('lines')
    return len(order_ids)
```

A view reads the stream through the client and hands it to Django's
`StreamingHttpResponse`:

```python
from django.http import StreamingHttpResponse

from django_dbos.client import get_client


def export_lines(request, workflow_id):
    lines = get_client().read_stream(workflow_id, 'lines', timeout_seconds=30)
    return StreamingHttpResponse((f'{line}\n' for line in lines), content_type='text/plain')
```

`read_stream` yields each value as soon as it is written and stops when the
stream is closed or the workflow ends. Reading again starts from the first
value: pass `offset=` to resume where a previous reader stopped, or use
`read_stream_offset(workflow_id, key, offset)` to wait for a single value.
`timeout_seconds` limits the wait for each value and raises
`dbos.error.DBOSStreamTimeoutError` when it expires; without it, a reader
waits forever on a stream that is never closed.

!!! tip
    Under WSGI, a streaming response holds a worker thread until the stream
    ends. Under ASGI, use an async view with `read_stream_async`, which
    returns an async iterator `StreamingHttpResponse` accepts as is.

A workflow can also read another workflow's stream with
`ctx.read_stream(...)` / `ctx.read_stream_offset(...)`.

## Child workflows: `start_child_workflow`

Starts another DBOS workflow without waiting for it, returning a handle. Use
it to fan out work that should be tracked, retried and recovered
independently of the parent — as opposed to `ctx.run_step`, which ties the
call to the parent's own checkpointing.

The child must itself be a DBOS workflow function, declared with `@DBOS.workflow`
(the same decorator django-dbos uses internally to turn a `@task` into a
workflow):

```python
from dbos import DBOS


@DBOS.workflow()
def resize_image(attachment_id: int, size: str) -> str: ...


@dbos_params(durable_operations=True)
@task
def process_upload(attachment_id: int) -> None:
    ctx = get_dbos_context()
    handles = [ctx.start_child_workflow(resize_image, attachment_id, size) for size in ('thumbnail', 'medium', 'large')]
    # optional: block until all children are done and collect their results
    for handle in handles:
        handle.get_result()
```

If you don't need the result, drop the `get_result()` loop — the children
keep running and get recovered independently even if the parent finishes or
crashes first.

### Results in completion order: `wait_first` / `as_completed`

The loop above reads the results in the order the children were started: a
slow first child holds back the results of the others, even when they are
already done. `as_completed` yields each handle as soon as its child
finishes:

```python
from django_dbos import as_completed


@dbos_params(durable_operations=True)
@task
def process_upload(attachment_id: int) -> None:
    ctx = get_dbos_context()
    handles = [ctx.start_child_workflow(resize_image, attachment_id, size) for size in ('thumbnail', 'medium', 'large')]
    for handle in as_completed(handles):
        url = handle.get_result()  # the child's result; raises if the child failed
        ctx.run_step(publish_rendition, attachment_id, url)
```

It is a loop over `ctx.wait_first(handles)`, which waits for the first of
several workflows to finish and returns its handle. Call it directly for
more control, such as stopping at the first result:

```python
fastest = ctx.wait_first(handles)
```

Each wait is checkpointed: after a crash, the replay goes through the
children in the same order. Both check the children's status at DBOS'
default interval (one second today); `polling_interval_sec=` changes it.

## Crashes and failing steps

What happens to a workflow stopped halfway depends on why it stopped. Take
`export_orders` from the streams example above, with four orders: the first
order's line is rendered and written to the stream, then the workflow stops
while rendering the second one.

| The workflow stopped because... | What happens next |
| --- | --- |
| the worker crashed or was redeployed | DBOS recovers it: the function replays from the top, but every operation already checkpointed returns its recorded result instead of running — `list_order_ids`, the first `render_order_line` and the first `write_stream` included, so the first order's line is not written to the stream twice. Only the second order is rendered again. |
| it was cancelled, then resumed | The same: it continues after the last completed step. |
| `render_order_line` raised | The task is `FAILED` and stays so: recovery is for crashes, and resuming doesn't apply to a failed workflow. Enqueueing the task again starts a new workflow, from the first order. |

A step that can fail transiently (a network call, a locked row) should
therefore retry: see [Retrying a step](#retrying-a-step).

### Restarting a failed workflow from its failing step

`fork_workflow` copies a workflow and runs the copy from a given step: the
steps before it return the original's recorded results without running, and
the values they wrote to streams are copied too. Once the cause of the
failure is fixed:

```python
from django_dbos.client import get_client

handle = get_client().fork_workflow(result.id, start_step=4)
forked = export_orders.get_result(handle.workflow_id)  # a task result like any other
```

The step to start from is in the **Step ID** column of the workflow's [admin](admin.md)
page, where the failing step shows its error. Stream writes count as steps:
here `list_order_ids` is step 1, the first `render_order_line` step 2, its
`write_stream` step 3, and the failing `render_order_line` step 4. The fork
re-renders orders 2 to 4, and its stream holds all four lines.

The fork is a new execution with its own ID: read its result and its stream
through `handle.workflow_id`. The admin's **Fork** button forks from step 0,
which runs the whole workflow again.
