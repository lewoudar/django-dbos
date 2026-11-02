---
icon: lucide/gauge
---

# Worker capacity

The `dbos status` management command reports on worker capacity so you can
decide whether to scale your workers up, down or leave them as-is:

```bash
python manage.py dbos status
```

```
Queue "default"  [OK]
  Backlog: 2 (delayed: 0)   Running: 3   Live workers: 1
  Oldest waiting workflow: 12s
  Concurrency: worker=4 global=unlimited

Queue "per_user"  [OK]
  Backlog: 0 (delayed: 0)   Running: 3 (1 orphaned)   Live workers: 1
  Concurrency: worker=unlimited global=unlimited
  Partition concurrency: worker=unlimited global=1
  Partition rate limit: 5 per 60s

Workers:
  3f2a...: worker-1 pid 4212, queues default,per_user, CPU 12.0%, RSS 184.5 MB
  9c1e...: NO HEARTBEAT (worker down, or django_dbos migrations not applied)

No workflow exhausted its recovery attempts.
```

## What it measures

Queue and executor figures come from the DBOS system database, read through
a `DBOSClient` (the command never launches DBOS: it can run anywhere, a cron
job or a web instance included). Only the *active* workflows — waiting or
running, i.e. the backlog itself — are listed and aggregated:

- **Backlog** per queue: workflows waiting for a worker (DBOS status
  `ENQUEUED`), and the age of the oldest one. Deferred tasks whose
  `run_after` time has not come yet (`DELAYED`) are counted apart.
- **Running** per queue: workflows being executed (DBOS status `PENDING`),
  plus the longest time one of them waited in the queue before starting.
  Those whose executor has no live worker are counted apart as
  **orphaned**: their worker died, they make no progress until a worker
  with that executor id recovers them (see
  [Executor identity](configuration.md#executor-identity)).
- **Live workers** per queue: workers with a fresh
  [heartbeat](#workers-heartbeats) that serve the queue (all queues, or the
  ones given to [`dbos worker --queues`](configuration.md#dedicated-workers)).
- **Declared limits** as stored on the queue, so you can compare load with
  capacity at a glance: worker/global concurrency (`unlimited` when not
  declared), and for the queues that declare them the partition
  concurrency and the rate limits.
- **Per-executor load** (`executor_id` on workflow rows). Note that executors
  which did not set a specific ID all show up as `"local"`.
- Workflows that **exhausted their recovery attempts** (stuck tasks).

## Advice

Each queue gets a simple heuristic:

| Advice | Meaning |
| --- | --- |
| `SCALE_UP` | Fewer than `MIN_WORKERS` (1 by default) live workers serve the queue, even an idle one; or workflows wait and no live worker serves the queue; or the oldest waiting workflow has been waiting for more than `SCALE_UP_BACKLOG_AGE_SECONDS` (60 by default): the queue is not drained fast enough. |
| `SCALE_DOWN` | Nothing waiting, at most `SCALE_DOWN_MAX_RUNNING` (0 by default) workflows running and more than `MIN_WORKERS` live workers: an idle queue with workers to spare. |
| `OK` | Everything looks healthy. |

### Thresholds

`OPTIONS["CAPACITY"]` accepts these keys, all optional:

| Key | Default | Meaning |
| --- | --- | --- |
| `SCALE_UP_BACKLOG_AGE_SECONDS` | `60` | Advise `SCALE_UP` once the oldest waiting workflow has waited longer than this. |
| `SCALE_DOWN_MAX_RUNNING` | `0` | Running workflows an idle queue may still have to get `SCALE_DOWN`. |
| `MIN_WORKERS` | `1` | Floor of live workers per queue (see [Scaling to zero](#scaling-to-zero)). |
| `ADVISOR` | built-in heuristic | A callable, or its dotted path, replacing the advice function (see [Your own heuristic](#your-own-heuristic)). Global only. |

Set the thresholds globally in `OPTIONS["CAPACITY"]`, and per queue under the
queue's own `capacity` option (which is never sent to DBOS). Every
threshold is a non-negative number; `SCALE_DOWN_MAX_RUNNING` and
`MIN_WORKERS` count workflows and workers, so they must be integers:

```python
TASKS = {
    'default': {
        'BACKEND': 'django_dbos.backends.DBOSBackend',
        'OPTIONS': {
            'CAPACITY': {
                'SCALE_UP_BACKLOG_AGE_SECONDS': 30,
                'SCALE_DOWN_MAX_RUNNING': 1,
                'MIN_WORKERS': 2,
            },
            'QUEUES': {
                'default': None,
                'emails': {'worker_concurrency': 4, 'capacity': {'SCALE_UP_BACKLOG_AGE_SECONDS': 300}},
            },
        },
    },
}
```

The thresholds applied to each queue are part of the report (`thresholds`),
so a dashboard can show *why* a queue got its advice.

### Scaling to zero

`MIN_WORKERS` is a floor: an idle queue with exactly that many live workers
stays at `OK` rather than `SCALE_DOWN`, and one with fewer — after a crash,
or a worker stopped by hand — gets `SCALE_UP`, so following the advice never
stops the last worker and brings it back if it is gone. Set it to `0` to
allow scaling a queue down to no worker at all, knowing that:

- the `@cron` schedules only fire inside a running worker: with none left,
  they stop, and since nothing gets enqueued, no `SCALE_UP` brings a worker
  back;
- a task enqueued meanwhile waits until your autoscaling script reads
  `SCALE_UP` (advised as soon as a workflow waits with no live worker) and
  a machine has booted;
- PaaS auto-start features (Fly.io's `auto_start_machines`, for instance)
  are triggered by incoming HTTP requests, which a worker never receives:
  the script polling the advice must start the machine itself.

Worker counts come from the heartbeats: if they cannot be read (the
`django_dbos` migrations are not applied), every queue counts 0 live
workers, hence `SCALE_UP` everywhere unless `MIN_WORKERS` is `0`.

### Your own heuristic

Replace the advice function entirely with a callable of your own, set as
`OPTIONS["CAPACITY"]["ADVISOR"]` (the callable itself or its dotted path;
a queue's `capacity` option does not accept it), or as the `advisor=` argument of
[`collect()`](#from-python) when you build the report from Python:

```python
# in the DBOS backend's OPTIONS
'CAPACITY': {'ADVISOR': 'myproject.scaling.advise'},
```

It is called once per queue, with the queue's name, its figures and the
thresholds configured for it, and returns the advice shown in the report:

```python
from django_dbos.capacity import QueueMetrics, Thresholds, default_advisor


def advise(queue_name: str, metrics: QueueMetrics, thresholds: Thresholds) -> str:
    """Also scale up a queue running at its global concurrency limit."""
    limit = metrics['global_concurrency']
    if limit and metrics['running'] >= limit:
        return 'SCALE_UP'
    return default_advisor(queue_name, metrics, thresholds)  # the built-in rules otherwise
```

`metrics` holds the queue's figures (`QueueMetrics`):

| Key | Type | Meaning |
| --- | --- | --- |
| `enqueued` | `int` | Workflows waiting for a worker. |
| `delayed` | `int` | Deferred workflows whose `run_after` time has not come yet. |
| `running` | `int` | Workflows being executed. |
| `orphaned` | `int` | Running workflows whose executor has no live worker: interrupted, waiting for recovery. |
| `backlog_age_seconds` | `float` or `None` | How long the oldest waiting workflow has been waiting; `None` when none waits. |
| `max_queue_wait_ms` | `int` or `None` | The longest time a running workflow waited in the queue before starting. |
| `worker_concurrency` | `int` or `None` | The queue's declared per-worker concurrency. |
| `global_concurrency` | `int` or `None` | The queue's declared global concurrency. |
| `partition_concurrency` | `int` or `None` | The queue's declared global concurrency per partition. |
| `partition_worker_concurrency` | `int` or `None` | The queue's declared per-worker concurrency per partition. |
| `limiter` | `dict` or `None` | The queue's declared rate limit, `{"limit": int, "period": float}`. |
| `partition_limiter` | `dict` or `None` | The queue's declared rate limit per partition, same shape. |
| `workers` | `int` | Live workers (fresh heartbeat) serving the queue. |

`thresholds` (`Thresholds`) holds the [thresholds](#thresholds) applied to
the queue, with lowercase keys — `scale_up_backlog_age_seconds`,
`scale_down_max_running` and `min_workers` — so your advisor can honour the settings rather
than hard-code its limits. `default_advisor` is the built-in heuristic,
handy to keep its rules and only add your own.

The advice is any string: `SCALE_UP`, `SCALE_DOWN` and `OK` are the built-in
ones, and the report shows a value of your own as it is.

!!! warning
    `collect()` is a **snapshot**. A single `SCALE_DOWN` reading only means
    the queue was idle at that instant — smooth several samples over a few
    minutes before removing a worker. django-dbos keeps no history on
    purpose; that logic belongs to your autoscaling script or dashboard.

## Workers (heartbeats)

The system database tells you who *worked* recently, not who is *alive*: an
idle worker leaves no trace there, yet it is exactly the one you want to
know about before scaling down. Running workers therefore push a heartbeat
row into the Django database (the `WorkerHeartbeat` model, hence the
`django_dbos` migration) — the same push model as [DBOS Conductor](https://docs.dbos.dev/conductor/overview), with your
database as the channel. Any process with the ORM can read it: this command,
the admin, an autoscaling script.

```bash
# optional: adds CPU/memory to the heartbeat
uv add "django-dbos[metrics]"
# or
pip install "django-dbos[metrics]"
```

Workers report their host, pid, served queues (and CPU/memory) every 10
seconds and delete their row on clean shutdown. A row not refreshed for 30
seconds is considered stale (`alive: false` in the JSON output, `STALE` in
the human output): the worker died without cleaning up. An executor id seen
on active workflows without any heartbeat row is flagged `NO HEARTBEAT`.

Several worker processes may share an `executor_id` (DBOS' default is
`"local"` for all of them); each worker run gets its own heartbeat row,
identified by a UUID (`id`) and labelled `executor_id@hostname:pid`
(`worker_id`), so they are always told apart. A worker restarted after a
crash with the same hostname and pid (pid 1 in a container) gets a fresh
row; the old one shows as stale until it is purged a day later. See
[Executor identity](configuration.md#executor-identity) to name your
instances.

!!! warning
    CPU/memory metrics only cover the worker process itself. Machine-level
    resources (and several machines at once) belong to a monitoring stack
    (Prometheus node/process exporters, Datadog, ...).

## Options

```bash
# machine-readable output (for alerting, cron checks, dashboards):
python manage.py dbos status --json

# restrict to specific queues:
python manage.py dbos status --queues=emails,reports
```

## From Python

`dbos status` is a thin wrapper around `django_dbos.capacity.collect()`,
which returns the report as a dict, the same structure as `--json`. Call it
from a Prometheus exporter, a health endpoint or your own autoscaling logic:

```python
from django_dbos.capacity import collect

report = collect(queues=['emails'])  # all declared queues by default
emails = report['queues']['emails']  # a QueueMetrics, plus its thresholds and advice
if emails['advice'] == 'SCALE_UP':
    ...
```

The report (a `CapacityReport`) holds `queues` (queue name → its `QueueMetrics`, `thresholds`
and `advice` included), `executors` (executor ID → running and enqueued
counts), `workers` (the heartbeats, stale ones included) and
`max_recovery_attempts_exceeded`. `collect(advisor=...)` replaces the
configured advisor for one call; `advise_queue(metrics,
scale_up_backlog_age_seconds=..., scale_down_max_running=..., min_workers=...)` is the
built-in heuristic with explicit thresholds.
