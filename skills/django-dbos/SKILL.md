---
name: django-dbos
description: Use django-dbos, a DBOS-backed backend for Django's django.tasks framework (durable background tasks, cron scheduling, admin pages, worker-capacity reporting). Use this skill when adding or reviewing background tasks, `@task`/`@cron` functions, queue configuration, or `TASKS = {..., "BACKEND": "django_dbos.backends.DBOSBackend"}` in a Django project, or when the project already depends on django-dbos and you're asked to add a durable workflow, a scheduled job, or to test/deploy one.
license: MIT
metadata:
  author: lewoudar
  version: "1.0.0"
  abstract: Guide for using django-dbos in a Django project — declaring tasks, durable workflows, cron schedules, testing, configuration and deployment — from the consumer side, not from django-dbos's own codebase.
---

# django-dbos

DBOS-backed backend for Django's [`django.tasks`](https://docs.djangoproject.com/en/stable/ref/tasks/)
framework. If you already know Django's tasks API, you already know most of
django-dbos: tasks are `@task`-decorated functions, enqueued with
`.enqueue()`/`.aenqueue()`, and results are standard `TaskResult` objects.
The difference is durability: every execution is a DBOS workflow recorded in
a system database, so a worker crash resumes the task instead of losing it,
and `@dbos_params(durable_operations=True)` unlocks multi-step durable
workflows (steps, durable sleep, events, messages, child workflows).

This skill is for using django-dbos in a Django project that depends on it.
It is not about developing django-dbos itself.

## When to reach for what

| Need | Use |
| --- | --- |
| A background job that must survive a crash, no special semantics needed | `@task` alone (single checkpointed step) |
| Retry the task function itself on failure | `@dbos_params(step_retries_allowed=True, ...)` |
| A multi-stage job: several checkpoints, a durable sleep, waiting on an external signal | `@dbos_params(durable_operations=True)` + `get_dbos_context()` or `@DBOS.step`/`@DBOS.workflow` |
| Run on a schedule | `@cron(...)` stacked on `@task` |
| Reject duplicate concurrent runs | `@dbos_params(deduplication_id=...)` |
| Cap total wall-clock time | `@dbos_params(workflow_timeout=...)` |

## Setup

```python
INSTALLED_APPS = [..., 'django_dbos']

TASKS = {
    'default': {
        'BACKEND': 'django_dbos.backends.DBOSBackend',
        'QUEUES': ['default', 'emails'],  # must also be declared in DJANGO_DBOS["QUEUES"]
    },
}

DJANGO_DBOS = {
    'QUEUES': {'default': None, 'emails': {'worker_concurrency': 4}},
}
```

```bash
python manage.py migrate        # django-dbos ships one small model (worker heartbeats)
python manage.py dbos_migrate    # creates/migrates the DBOS system database
python manage.py dbos_worker     # executes tasks — see "Processes" below
```

Tasks live in each app's `tasks.py` (or `dbos_tasks.py`); the worker
autodiscovers them on startup, the same way Django autodiscovers `admin.py`.

## Processes — read before touching config

Two kinds of processes, never confuse them:

- **Web processes, shell, management commands** — everything except
  `dbos_worker` — only talk to the DBOS *system database* through a
  `DBOSClient` (enqueue, read results, admin pages, `dbos_status`). They
  **never execute a task**.
- **`dbos_worker`** is the only process that runs task code: it registers
  every known task as a DBOS workflow, then launches queue workers, the cron
  scheduler and crash recovery.

If a task "does nothing" when enqueued, the first thing to check is whether
`dbos_worker` is running and has that task's module imported (app installed,
`tasks.py` not shadowed by an import error).

## Declaring tasks

```python
from django.tasks import task


@task
def send_welcome_email(user_id: int) -> None: ...


result = send_welcome_email.enqueue(user_id)  # sync
result = await send_welcome_email.aenqueue(user_id)  # async views

result.status  # TaskResultStatus.READY / RUNNING / SUCCESSFUL / FAILED
result.return_value  # only when SUCCESSFUL
```

- `.using(run_after=timezone.now() + timedelta(hours=1)).enqueue()` for deferred tasks — needs an *aware* datetime.
- `@task(priority=50)` maps onto DBOS queue priority (-100..100, higher first).
- Task functions use the ORM as usual; django-dbos recycles connections
  around each execution (`CONN_MAX_AGE` honoured), so don't hold a connection
  open across a long task by hand.

### `@dbos_params` — DBOS-specific options

Order relative to `@task` doesn't matter, and it's a no-op on non-DBOS
backends (handy for tests running against Django's immediate backend).

```python
from django_dbos import dbos_params


@dbos_params(
    step_retries_allowed=True,  # retry the task function on failure
    step_max_attempts=5,
    step_backoff_rate=2.0,
    deduplication_id='nightly-cleanup',  # reject duplicate concurrent enqueues
    workflow_timeout=3600,  # seconds, wall clock from worker pickup
    max_recovery_attempts=10,  # DBOS recovery after worker crashes
)
@task
def cleanup(): ...
```

| Option | Default | Description |
| --- | --- | --- |
| `step_retries_allowed` | `False` | Retry the task function on failure. |
| `step_max_attempts` | `3` | Max attempts when step retries are enabled. |
| `step_interval_seconds` | `1.0` | Delay between step retries. |
| `step_backoff_rate` | `2.0` | Multiplier applied to the interval after each failure. |
| `deduplication_id` | `None` | While a workflow with this ID is enqueued or running, further enqueues raise. |
| `queue_partition_key` | `None` | Partition key for partitioned queues. |
| `workflow_timeout` | `None` | Cancelled past this deadline; result reports `FAILED`. Queue wait time doesn't count. |
| `max_recovery_attempts` | DBOS default | Recovery attempts after a worker crash. |
| `durable_operations` | `False` | Run the function at workflow level — see below. |

## Durable workflows (`durable_operations=True`)

By default the whole task function is one checkpointed step: on crash it
restarts from the top. For a multi-stage task where restarting from zero is
the wrong kind of retry, opt into workflow-level execution:

```python
from django_dbos import dbos_params, get_dbos_context


@dbos_params(durable_operations=True)
@task
def onboard_user(user_id: int) -> None:
    ctx = get_dbos_context()
    ctx.run_step(send_welcome_email, user_id)  # checkpointed
    ctx.sleep(86400)  # durable sleep, worker can restart
    ctx.run_step(send_followup_email, user_id)
```

!!! critical
    A `durable_operations` function MUST be deterministic and re-runnable:
    DBOS replays it from the top after a crash, skipping already-checkpointed
    primitives. Never put a network call, random value, `timezone.now()`, or
    any other side effect directly in the function body — always behind
    `run_step`/`@DBOS.step`.

`get_dbos_context()` returns:

| Method | Use for |
| --- | --- |
| `run_step(func, *args, name=...)` / `run_step_async(...)` | Checkpoint a plain function call. Prefer this for functions you don't own, or when the step name must be computed per call (e.g. `f'email-{user_id}'` in a loop). |
| `sleep(seconds)` / `sleep_async(...)` | Durable sleep — the workflow stops, no thread/worker slot held, resumes even across a restart. |
| `set_event(key, value)` / `get_event(workflow_id, key, timeout_seconds=60)` | Publish/read a value on a workflow. `get_event` works from *anywhere*, not just inside a workflow — good for a view polling progress. |
| `send(workflow_id, message, topic=None)` / `recv(topic=None, timeout_seconds=60)` | Point-to-point, consumed-once message. `recv` blocks the *calling* workflow (must be called from inside one); typical use: pause for a human decision (e.g. an admin action calls `DBOS.send(workflow_id, True, topic='approval')`). |
| `start_child_workflow(func, *args)` | Start another DBOS workflow (must be `@DBOS.workflow`-decorated) without waiting; returns a handle (`handle.get_result()`). Independent checkpointing/recovery from the parent — not for calling another `@task`, just use `.enqueue()` for that. |

If you own the step function, DBOS's own `@DBOS.step` decorator works too —
call it with normal arguments, no context object needed, as long as the call
happens inside a `durable_operations` task (workflow-level execution is what
makes the checkpointing active):

```python
from dbos import DBOS


@DBOS.step()
def send_welcome_email(user_id: int) -> None: ...


@dbos_params(durable_operations=True)
@task
def onboard_user(user_id: int) -> None:
    send_welcome_email(user_id)  # already checkpointed
```

Both styles (`ctx.run_step` and `@DBOS.step`) checkpoint the same way and can
be mixed freely in the same task.

## Cron scheduling

```python
from django_dbos import cron


@cron('*/5 * * * *')  # standard 5-field crontab; 6-field (leading seconds) also accepted
@task
def cleanup_expired_sessions(): ...
```

- Scheduled tasks **must take no arguments** — validated at module load.
- Schedules are managed by DBOS (registered by `dbos_worker` on startup,
  idempotently) and survive restarts/deploys; the code is the source of
  truth (schedules are created/updated/deleted to match `@cron` decorators
  on each worker boot).
- Run `dbos_worker` without `--no-scheduler` for it to manage schedules (skip
  it on secondary workers behind a load balancer if you run one dedicated
  scheduler instance).
- Every `@dbos_params` option applies to scheduled firings too (they run the
  same workflow `.enqueue()` would).
- Optional Sentry cron monitors: `uv add django-dbos[sentry]`, then
  check-ins are sent automatically per scheduled firing (not for ad-hoc
  `.enqueue()` calls). Disable per task with `@cron(..., sentry_monitor_config=None)`.

## Testing

```bash
uv add --group test "django-dbos[test]" pytest-django
```

Three ways to test, in order of how much you want to exercise:

```python
from django_dbos.test import run_task, arun_task


# 1. Run the task body directly, bypassing the backend entirely
def test_send_email():
    assert run_task(send_email, 'kevin@example.com') == 'sent to kevin@example.com'
```

```python
# 2. dbos_inline fixture: enqueue runs synchronously in-process, no system DB / worker.
#    Real TaskResult, real queue validation against DJANGO_DBOS["QUEUES"].
def test_send_email(dbos_inline):
    result = send_email.enqueue('kevin@example.com')
    assert result.status == TaskResultStatus.SUCCESSFUL
    assert result.return_value == 'sent to kevin@example.com'
```

```python
# 3. dbos_system fixture: real DBOS semantics (step retries, durable sleep,
#    schedules, crash recovery) — the test process IS the worker.
def test_durable_sleep(dbos_system):
    result = long_task.enqueue()
    # poll result.refresh() until it succeeds
```

`dbos_inline` silently runs deferred tasks and `durable_operations` code
anyway (it can't reproduce those semantics) — pass
`--dbos-inline-strict` to turn that into a hard error instead of a silent gap
between test and production behaviour. Prefer `dbos_system` when the test is
actually about durability.

## Configuration essentials (`DJANGO_DBOS` setting)

- **System database**: derived by default from the Django default
  connection (`dbos_<dbname>` on Postgres, `<name>-dbos.sqlite` for SQLite —
  dev only). Override with `DJANGO_DBOS['SYSTEM_DATABASE_URL']` or the
  `DBOS_SYSTEM_DATABASE_URL` env var. It only carries DBOS' own tables — app
  data stays in the regular Django database(s).
- **Queues**: every queue name used by a task (`queue_name=` / `TASKS[alias]['QUEUES']`)
  must be declared under `DJANGO_DBOS['QUEUES']` (worker/global concurrency,
  rate limiter, scaling thresholds).
- **Serialization**: pickle by default — fine, but treat the system database
  as trusted (anyone who can write to it can run code in your workers).
  Switch to `'SERIALIZER': 'dbos.DBOSPortableJSONSerializer'` when task
  args/results are plain data.
- **`RUN_MIGRATIONS`** (default `True`): a worker creates/migrates the system
  database on every launch — convenient in dev, wrong in production (DDL
  rights on the worker role, migration races on first boot of a new
  version). Set to `False` and run `python manage.py dbos_migrate` as an
  explicit release step, next to `migrate`.
- **`EXECUTOR_ID`**: give each worker *instance* a stable identity (survives
  its own restarts, distinct from other instances) — crash recovery and
  capacity reports are keyed by it. Falls back to `DBOS__VMID` env, then
  `"local"`.

## Admin & operability

- Django admin gets **DBOS workflows** and **DBOS schedules** pages
  (superuser-only — they expose task inputs/outputs and can cancel/fork/
  delete workflows). Disable with `DJANGO_DBOS['ADMIN_ENABLED'] = False` if
  superusers aren't fully trusted with that.
- `python manage.py dbos_status` (or `--json`) reports queue backlog,
  running counts and a scale up/down/OK advice per queue, plus worker
  heartbeats (who's alive, `[metrics]` extra adds CPU/RSS) and workflows that
  exhausted their recovery attempts. Safe to run anywhere (it's a
  `DBOSClient`, not a worker) — good for a cron health check or an
  autoscaler.

## Deployment checklist

1. Two process kinds, scaled independently: web/commands (client only) vs.
   `dbos_worker` (executes tasks).
2. A release step running `migrate` then `dbos_migrate` before the new
   version serves, with `RUN_MIGRATIONS=False` so workers only verify the
   schema.
3. A stable `EXECUTOR_ID` per worker instance (platform instance id/hostname
   — never a pid, which changes every restart).
4. Decide the system database URL and serializer deliberately (see above).
5. `DEBUG=False` — error pages otherwise print local variables, which can
   include the system database URL held by the SDK.

## Common pitfalls

- Enqueuing a task and nothing happens → check `dbos_worker` is running and
  actually imports that app's `tasks.py` (autodiscovery failure is silent
  from the enqueuer's point of view).
- `queue_name` used by a task but not declared in `DJANGO_DBOS['QUEUES']` →
  rejected at enqueue/validation time.
- A `@cron` task declared with parameters → rejected at module load, not at
  runtime.
- Non-deterministic code (API calls, `timezone.now()`, randomness) directly
  in a `durable_operations` function body → replay after a crash re-executes
  it with different results; wrap it in `run_step`/`@DBOS.step`.
- Treating `dbos_inline`'s handling of deferred/`durable_operations` tasks as
  proof they work — it silently no-ops the semantics unless
  `--dbos-inline-strict` is passed; use `dbos_system` to actually exercise
  them.

## Further reading

The package ships full documentation under `docs/` (built with zensical);
once the repository is public it's also served at
<https://lewoudar.github.io/django-dbos/>. Pages: `configuration.md`,
`tasks.md`, `workflows.md` (every durable primitive with a worked example),
`cron.md`, `admin.md`, `status.md`, `testing.md`, `deployment.md`.
