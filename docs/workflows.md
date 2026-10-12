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

`get_event` returns `None` if it times out waiting, rather than raising —
useful here, since "no value yet" and "still on the first stage" both read as
legitimate states while the workflow starts up.

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
