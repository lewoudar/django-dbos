# Testing

django-dbos ships a pytest plugin (loaded automatically as soon as
django-dbos is installed). It provides two fixtures to test tasks through the
tasks API.

## Running task functions directly

The simplest way to test a task body is to bypass the backend entirely:

```python
from django_dbos.test import run_task, arun_task


def test_send_email():
    result = run_task(send_email, 'kevin@example.com')
    assert result == 'sent to kevin@example.com'
```

`run_task` executes the function synchronously in-process (async task functions
are bridged with `async_to_sync`).

## Inline mode: `dbos_inline`

The `dbos_inline` fixture patches the DBOS backend so enqueued tasks execute
synchronously in-process — no system database, no worker. The enqueue still
returns a standard `TaskResult`, so you can assert on status and return value:

```python
def test_send_email(dbos_inline):
    result = send_email.enqueue('kevin@example.com')
    assert result.status == TaskResultStatus.SUCCESSFUL
    assert result.return_value == 'sent to kevin@example.com'
```

Features the inline mode cannot reproduce (deferred tasks, durable
operations) execute anyway; pass `--dbos-inline-strict` to turn them into
errors instead, so your tests don't silently diverge from production
semantics.

## Real system database: `dbos_system`

When you need real semantics (step retries, durable sleeps, schedules, crash
recovery), use `dbos_system`: the fixture launches DBOS in-process on a system
database — the current test process acts as the worker.

```python
def test_durable_sleep(dbos_system):
    result = long_task.enqueue()
    # poll result.refresh() until it succeeds
```

The system database is a SQLite file by default; pass
`--dbos-system-db=postgres` to run a PostgreSQL container via
`testcontainers[postgres]` (skipped if Docker is unavailable). With the
default `--dbos-system-db=auto`, the PostgreSQL container is used when
possible and SQLite is the fallback.

## structlog

django-dbos logs through [structlog](https://www.structlog.org/) under the
`django_dbos` logger. Use structlog's
[testing utilities](https://www.structlog.org/en/stable/testing.html) to assert
on log output:

```python
from structlog.testing import capture_logs


def test_enqueue_logs():
    with capture_logs() as logs:
        send_email.enqueue('kevin@example.com')
    assert any(event == 'enqueued task' for event in [log['event'] for log in logs])
```
