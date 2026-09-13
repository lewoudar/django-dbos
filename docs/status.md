# Worker capacity

The `dbos_status` management command reports on worker capacity so you can
decide whether to scale your workers up, down or leave them as-is:

```bash
python manage.py dbos_status
```

```
Queue "default"  [OK]
  Backlog: 2 (delayed: 0)   Running: 3
  Oldest waiting workflow: 12s
  Declared concurrency: worker=4 global=None

Workers:
  3f2a...: pid 4212, CPU 12.0%, RSS 184.5 MB

Executors:
  3f2a...: running=3 enqueued=2

No workflow exhausted its recovery attempts.
```

## What it measures

Everything comes from the DBOS system database — no agent, nothing to deploy:

- **Backlog** per queue: workflows enqueued and waiting (ENQUEUED/PENDING), and
  the age of the oldest waiting workflow.
- **Running** per queue, plus the maximum queue wait time of running
  workflows.
- **Declared concurrency** (worker/global) as stored on the queue, so you can
  compare load with capacity at a glance.
- **Per-executor load** (`executor_id` on workflow rows). Note that executors
  which did not set a specific ID all show up as `"local"`.
- Workflows that **exhausted their recovery attempts** (stuck tasks).

## Advice

Each queue gets a simple heuristic:

| Advice | Meaning |
| --- | --- |
| `SCALE_UP` | The oldest waiting workflow has been waiting for more than 60 seconds: the queue is not drained fast enough. |
| `SCALE_DOWN` | Nothing running and nothing waiting: an idle queue. |
| `OK` | Everything looks healthy. |

The thresholds are simple on purpose. For real capacity planning, use the JSON
output (below) with your monitoring system.

## Workers (heartbeats)

Running workers push a heartbeat (process metrics through Django's cache) —
this is the same push model as DBOS Conductor, with your cache as the channel:

```bash
uv add django-dbos[metrics]   # optional: adds CPU/memory to the heartbeat
```

Workers report their pid, CPU and memory every 10 seconds; the heartbeat
expires after 30 seconds, so a dead worker disappears (or shows up as
`NO HEARTBEAT`) by itself.

- With a **shared cache** (Redis, database cache), heartbeats are visible from
  any process: the command and the admin can report on every worker.
- With **LocMemCache**, each worker only sees its own heartbeat — the section
  degrades to "NO HEARTBEAT" for the others.

!!! warning
    CPU/memory metrics only cover the worker process itself. Machine-level
    resources (and several machines at once) belong to a monitoring stack
    (Prometheus node/process exporters, Datadog, ...).

## Options

```bash
# machine-readable output (for alerting, cron checks, dashboards):
python manage.py dbos_status --json

# restrict to specific queues:
python manage.py dbos_status --queues=emails,reports
```

!!! note
    The command loads tasks from installed apps and launches the DBOS
    singleton (it reads the system database). Run it from any machine that
    can reach the system database — it does not need to be a worker itself.
