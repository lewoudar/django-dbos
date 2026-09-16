# Deployment

django-dbos does not care where it runs: it needs a PostgreSQL system
database, one or more `dbos_worker` processes, and web processes that can
reach the same database. This page lists what a production setup should
settle, then shows the same setup on a few platforms.

## Checklist

1. **Two kinds of processes.** Web processes (and one-off commands) only talk
   to the system database; `dbos_worker` processes execute tasks. Scale them
   independently. See [Processes](configuration.md#processes).
2. **A release step** that runs before the new version starts serving:
   `python manage.py migrate` and `python manage.py dbos_migrate`, with
   `DJANGO_DBOS["RUN_MIGRATIONS"] = False` so workers verify the schema
   instead of altering it. See
   [Migrating the system database](configuration.md#migrating-the-system-database).
3. **A stable identity per worker instance**, through
   `DJANGO_DBOS["EXECUTOR_ID"]`: crash recovery and the capacity reports are
   keyed by it. Use the platform's instance name, never something that
   changes on every restart such as a pid. See
   [Executor identity](configuration.md#executor-identity).
4. **The system database URL.** Either let django-dbos derive it from the
   default Django database (same server, database `dbos_<name>` — the
   migrating role must be allowed to create it), or set
   `SYSTEM_DATABASE_URL` explicitly. Sharing the Django database with DBOS'
   tables in their own `dbos` schema is fine and avoids creating a second
   database on platforms where that is awkward.
5. **Serialization.** Decide whether pickle (the default) is acceptable for
   your system database, or switch to `dbos.DBOSPortableJSONSerializer`. See
   [Serialization](configuration.md#serialization).
6. `DEBUG = False`, as with any Django deployment.

A settings module covering the lot:

```python
import os

DJANGO_DBOS = {
    'NAME': 'myproject',
    'SYSTEM_DATABASE_URL': os.environ['DBOS_SYSTEM_DATABASE_URL'],
    'EXECUTOR_ID': os.environ.get('WORKER_INSTANCE_ID'),
    'RUN_MIGRATIONS': False,
    'SERIALIZER': 'dbos.DBOSPortableJSONSerializer',
    'QUEUES': {'default': None, 'emails': {'worker_concurrency': 4}},
}
```

The examples below only differ in *where* those values come from.

## fly.io

Two [process groups](https://fly.io/docs/launch/processes/) share one image:
`web` serves requests, `worker` runs `dbos_worker`. The release command runs
on a temporary Machine with the app's secrets before each deploy.

```toml
# fly.toml
app = "myproject"

[build]

[deploy]
  release_command = "sh -c 'python manage.py migrate && python manage.py dbos_migrate'"

[processes]
  web = "gunicorn myproject.wsgi"
  worker = "python manage.py dbos_worker"

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
DJANGO_DBOS = {
    'EXECUTOR_ID': os.environ.get('FLY_MACHINE_ID'),
    'RUN_MIGRATIONS': False,
    ...
}
```

Scale each group on its own: `fly scale count web=2 worker=3`. To drive the
`worker` count from the queues, poll `python manage.py dbos_status --json`
(or `capacity.collect()`) from a small scheduled job — smoothing the
readings over a few minutes before scaling down.

## Kubernetes

One Deployment per process kind, a Job for the release step. The Pod name is
the natural executor id; a StatefulSet makes it stable across restarts, a
Deployment makes it unique (recovery of a crashed Pod's workflows then
happens through DBOS' recovery attempts rather than by the same executor
relaunching).

```yaml
# worker deployment (excerpt)
containers:
  - name: worker
    image: myproject:latest
    command: ["python", "manage.py", "dbos_worker"]
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
    command: ["sh", "-c", "python manage.py migrate && python manage.py dbos_migrate"]
```

Stop workers with `SIGTERM` (the default): `dbos_worker` shuts DBOS down
cleanly and removes its heartbeat row. Give `terminationGracePeriodSeconds`
at least the duration of your longest step.

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
    command: sh -c "python manage.py migrate && python manage.py dbos_migrate"
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
    command: python manage.py dbos_worker
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

Any platform works as long as it can run the three commands (`migrate`,
`dbos_migrate`, `dbos_worker`), inject the database URL, and expose
something stable to name a worker instance (a hostname, a slot number, an
instance id). If it exposes nothing, DBOS' default `"local"` still works:
every worker recovers every interrupted workflow, and the capacity report
tells them apart by host and pid.
