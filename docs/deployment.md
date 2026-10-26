---
icon: lucide/server
---

# Deployment

django-dbos does not care where it runs: it needs a PostgreSQL system
database, one or more `dbos worker` processes, and web processes that can
reach the same database. This page lists what a production setup should
settle, then shows the same setup on a few platforms.

## Checklist

1. **Two kinds of processes.** Web processes (and one-off commands) only talk
   to the system database; `dbos worker` processes execute tasks. Scale them
   independently. See [Processes](configuration.md#processes).
2. **A release step** that runs before the new version starts serving:
   `python manage.py migrate`, which applies Django's migrations then DBOS'
   (the system database). Since the release step now owns the schema, set
   `OPTIONS["RUN_MIGRATIONS"] = False`: workers then stop migrating on
   launch, only check that the schema is up to date, and refuse to start
   otherwise. See
   [Migrating the system database](configuration.md#migrating-the-system-database).
3. **A stable identity per worker instance**, through
   `OPTIONS["EXECUTOR_ID"]`: crash recovery and the capacity reports are
   keyed by it. Use the platform's instance name, never something that
   changes on every restart such as a pid. See
   [Executor identity](configuration.md#executor-identity).
   Set `APPLICATION_VERSION` explicitly too: it decides which workers
   recover and dequeue a workflow, and the version DBOS computes otherwise
   ignores your task code. See
   [Application version](configuration.md#application-version).
4. **The system database URL.** Either let django-dbos derive it from the
   default Django database (same server, database `dbos_<name>` — the
   migrating role must be allowed to create it), or set
   `SYSTEM_DATABASE_URL` explicitly. Sharing the Django database with DBOS'
   tables in their own `dbos` schema is fine and avoids creating a second
   database on platforms where that is awkward.
   Behind a connection pooler in transaction mode, give the workers a direct
   URL, and keep Django's connections open in the workers (`CONN_MAX_AGE` or
   a pool): see [Database connections](configuration.md#database-connections).
5. **Serialization.** Decide whether pickle (the default) is acceptable for
   your system database, or switch to a safe serializer such as
   `django_dbos.serializers.MsgspecJSONSerializer`. See
   [Serialization](configuration.md#serialization).
6. `DEBUG = False`, as with any Django deployment.
7. **Run the deployment checks** in CI or in the release step:

    ```sh
    python manage.py check --deploy --fail-level WARNING
    ```

    django-dbos adds its own ([System checks](troubleshooting.md#system-checks)),
    among them `django_dbos.W003` (pickle) and `django_dbos.W004` (no
    executor ID) for points 3 and 5. Warnings do not fail `check` without
    `--fail-level WARNING`. Silence a warning you accepted with Django's
    [`SILENCED_SYSTEM_CHECKS`](https://docs.djangoproject.com/en/stable/ref/settings/#silenced-system-checks)
    setting, for example `['django_dbos.W003']`.

8. **A retention policy** for the workflow history, which DBOS never
   deletes on its own. See [Purging old workflows](#purging-old-workflows).

A settings module covering the lot:

```python
import os

TASKS = {
    'default': {
        'BACKEND': 'django_dbos.backends.DBOSBackend',
        'OPTIONS': {
            'NAME': 'myproject',
            'SYSTEM_DATABASE_URL': os.environ['DBOS_SYSTEM_DATABASE_URL'],
            'EXECUTOR_ID': os.environ.get('WORKER_INSTANCE_ID'),
            'APPLICATION_VERSION': '1',  # bumped on incompatible durable task changes
            'RUN_MIGRATIONS': False,
            'SERIALIZER': 'django_dbos.serializers.MsgspecJSONSerializer',  # django-dbos[msgspec]
            'QUEUES': {'default': None, 'emails': {'worker_concurrency': 4}},
        },
    },
}
```

The examples below only differ in *where* those values come from.

## Purging old workflows

DBOS keeps every workflow it ran (status, inputs, output, steps) in the
system database and never deletes them: left alone, the history grows
without bound. `purge_workflows` deletes the finished ones (`SUCCESS`,
`ERROR` and `CANCELLED`) completed more than a given duration ago; run it
from a scheduled task:

```python
from datetime import timedelta

from django.tasks import task
from django_dbos import cron, purge_workflows


@cron('0 0 * * *')  # every day at midnight
@task
def purge_old_workflows():
    purge_workflows(older_than=timedelta(days=30))
```

It deletes in batches (`batch_size=1000` by default) and returns the number
of workflows deleted. It leaves alone:

- workflows still enqueued, delayed or running, whatever their age;
- workflows that exhausted their recovery attempts
  (`MAX_RECOVERY_ATTEMPTS_EXCEEDED`): they are dead letters, to inspect and
  resume or delete from the [admin](admin.md);
- the children of a purged workflow, which are purged on their own once
  finished (a child may outlive its parent).

The purge task itself leaves one workflow per run, deleted by the later
runs. Pick `older_than` to cover how far back you want to inspect results
in the admin or through `get_result`: a purged task can no longer be
retrieved.

!!! note
    Worker heartbeats need no such cleanup: each worker run updates a single
    row, deleted on clean shutdown, and the rows of crashed workers are
    purged automatically after a day (see [Worker capacity](worker-capacity.md)).

## fly.io

Two [process groups](https://fly.io/docs/launch/processes/) share one image:
`web` serves requests, `worker` runs `dbos worker`. The
[release command](https://fly.io/docs/reference/configuration/#run-one-off-commands-before-releasing-a-deployment)
runs `migrate` on a temporary Machine, with the app's
secrets, before each deploy.

```toml
# fly.toml
app = "myproject"

[build]

[deploy]
  release_command = "python manage.py migrate --noinput"

[processes]
  web = "gunicorn myproject.wsgi"
  worker = "python manage.py dbos worker"

[http_service]
  internal_port = 8000
  processes = ["web"]
```

Settings:

- `fly postgres attach` sets `DATABASE_URL`. The simplest system database is
  the same database: set the secret
  `DBOS_SYSTEM_DATABASE_URL` to the same value with the `postgresql+psycopg://`
  scheme (DBOS keeps its tables in the `dbos` schema), or create a second
  database with `fly postgres connect` and point the secret at it.
- Each Machine has a stable `FLY_MACHINE_ID`, which survives restarts of that
  Machine and differs between Machines — exactly what `EXECUTOR_ID` needs:

```python
TASKS = {
    'default': {
        'BACKEND': 'django_dbos.backends.DBOSBackend',
        'OPTIONS': {
            'EXECUTOR_ID': os.environ.get('FLY_MACHINE_ID'),
            'RUN_MIGRATIONS': False,
            ...
        },
    },
}
```

Scale each group on its own: `fly scale count web=2 worker=3`. To drive the
`worker` count from the queues, poll `python manage.py dbos status --json`
(or `capacity.collect()`) from a small scheduled job and follow each
queue's advice: add machines on `SCALE_UP`, remove some on `SCALE_DOWN`.
Scale up as soon as the advice says so, since tasks are waiting; scale down
only once the advice has held for a few minutes, so a short lull doesn't
stop a machine you need again a minute later. See
[Worker capacity](worker-capacity.md).

`SCALE_DOWN` is never advised for a queue's last live worker: the
`MIN_WORKERS` [threshold](worker-capacity.md#thresholds) of
`OPTIONS["CAPACITY"]` (1 by default) is a floor, advising `SCALE_UP` below
it, so new tasks are picked up right away and, for example, the `@cron`
schedules keep firing.
Read [Scaling to zero](worker-capacity.md#scaling-to-zero) before setting
it to `0`.

## Kubernetes

A Deployment for the web processes, a **StatefulSet** for the workers, a
Job for the release step. The worker's executor id must survive a restart,
because a restarting worker only recovers the workflows of its own id. A
StatefulSet names its Pods `worker-0`, `worker-1`… and a crashed Pod comes
back under the same name, so its Pod name is a good executor id. A
Deployment gives a replacement Pod a new random name: the workflows of the
crashed Pod would stay pending, with no worker to recover them.

```yaml
# worker statefulset (excerpt)
containers:
  - name: worker
    image: myproject:latest
    command: ["python", "manage.py", "dbos", "worker"]
    env:
      - name: WORKER_INSTANCE_ID
        valueFrom:
          fieldRef:
            fieldPath: metadata.name
      - name: DBOS_SYSTEM_DATABASE_URL
        valueFrom:
          secretKeyRef:
            name: myproject
            key: dbos-system-database-url
```

```yaml
# release job (excerpt)
containers:
  - name: release
    image: myproject:latest
    command: ["python", "manage.py", "migrate", "--noinput"]
```

Stop workers with `SIGTERM` (the default): `dbos worker` removes its
heartbeat row, then waits **up to 10 seconds** for the tasks it is running to
finish before disconnecting. A task still running after that is interrupted
and recovered later from its last checkpoint — from the start for a task
without `durable_operations` — like after a crash. Keep
`terminationGracePeriodSeconds` above those 10 seconds (Kubernetes' default
of 30 is fine) so the shutdown is not cut short by a `SIGKILL`.

## Docker Compose

Useful for a single host or a staging box. The container name doubles as the
executor id; the `release` service runs the migrations once and exits.

```yaml
services:
  db:
    image: postgres:17
    environment:
      POSTGRES_PASSWORD: secret
  release:
    image: myproject
    command: python manage.py migrate --noinput
    environment: &env
      DATABASE_URL: postgresql://postgres:secret@db/myproject
      DBOS_SYSTEM_DATABASE_URL: postgresql+psycopg://postgres:secret@db/dbos_myproject
    depends_on: [db]
  web:
    image: myproject
    command: gunicorn myproject.wsgi
    environment: *env
    depends_on:
      release: {condition: service_completed_successfully}
  worker:
    image: myproject
    command: python manage.py dbos worker
    environment:
      <<: *env
      WORKER_INSTANCE_ID: worker-1
    depends_on:
      release: {condition: service_completed_successfully}
```

To run several workers on one host, give each its own service (and
`WORKER_INSTANCE_ID`) rather than `--scale worker=3`, which would make the
replicas indistinguishable.

## Anywhere else

Any platform works as long as it can run `migrate` before a
version goes live and `dbos worker` next to the web processes, inject the
database URL, and expose
something stable to name a worker instance (a hostname, a slot number, an
instance id). If it exposes nothing, DBOS' default `"local"` is fine for a
single worker only: with several, a worker that starts re-runs the
workflows still running on the others (see
[Executor identity](configuration.md#executor-identity)).
