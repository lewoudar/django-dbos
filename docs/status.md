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
  Backlog: 2 (delayed: 0)   Running: 3
  Oldest waiting workflow: 12s
  Declared concurrency: worker=4 global=None

Workers:
  3f2a...: worker-1 pid 4212, queues default,emails, CPU 12.0%, RSS 184.5 MB

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
- **Declared concurrency** (worker/global) as stored on the queue, so you can
  compare load with capacity at a glance.
- **Per-executor load** (`executor_id` on workflow rows). Note that executors
  which did not set a specific ID all show up as `"local"`.
- Workflows that **exhausted their recovery attempts** (stuck tasks).

## Advice

Each queue gets a simple heuristic:

| Advice | Meaning |
| --- | --- |
| `SCALE_UP` | The oldest waiting workflow has been waiting for more than `SCALE_UP_BACKLOG_AGE_SECONDS` (60 by default): the queue is not drained fast enough. |
| `SCALE_DOWN` | Nothing waiting and at most `SCALE_DOWN_MAX_RUNNING` (0 by default) workflows running: an idle queue. |
| `OK` | Everything looks healthy. |

### Thresholds

Set them globally in `OPTIONS["CAPACITY"]`, and per queue under the
queue's own `capacity` option (which is never sent to DBOS):

```python
TASKS = {
    'default': {
        'BACKEND': 'django_dbos.backends.DBOSBackend',
        'OPTIONS': {
            'CAPACITY': {
                'SCALE_UP_BACKLOG_AGE_SECONDS': 30,
                'SCALE_DOWN_MAX_RUNNING': 1,
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

### Your own heuristic

Replace the advice function entirely with a callable
`(queue_name, metrics, thresholds) -> str`, as a dotted path in the settings
or directly as an argument of `collect()`:

```python
# in the DBOS backend's OPTIONS
'CAPACITY': {'ADVISOR': 'myproject.scaling.advise'},
```

```python
def advise(queue_name, metrics, thresholds):
    if metrics['running'] >= (metrics['global_concurrency'] or 0) > 0:
        return 'SCALE_UP'
    return 'OK'
```

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
`django_dbos` migration) — the same push model as DBOS Conductor, with your
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

The command is a thin wrapper around `django_dbos.capacity.collect()`, which
returns the same structure as `--json` (`queues`, `executors`, `workers`,
`max_recovery_attempts_exceeded`) and accepts an `advisor=` callable. Call it
from a Prometheus exporter, a health endpoint or your own autoscaling logic;
`advise_queue(metrics, scale_up_backlog_age_seconds=..., scale_down_max_running=...)`
is the built-in heuristic if you want to reuse it.
