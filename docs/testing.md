---
icon: lucide/flask-conical
---

# Testing

django-dbos ships a pytest plugin, loaded automatically as soon as
django-dbos and pytest are installed. Its own dependencies come with the
`test` extra:

```bash
uv add --group test "django-dbos[test]" pytest-django
# or
pip install "django-dbos[test]" pytest-django
```

It provides two fixtures to test tasks through the tasks API. They expect
Django to be configured, which pytest-django does for you.

The examples on this page test this task:

```python
# myapp/tasks.py
import structlog
from django.tasks import task

logger = structlog.get_logger()


@task
def send_email(address: str) -> str:
    logger.info('email sent', to=address)
    return f'sent to {address}'
```

## Running task functions directly

The simplest way to test a task body is to bypass the backend entirely:

```python
from django_dbos.test import run_task

from myapp.tasks import send_email


def test_send_email():
    assert run_task(send_email, 'kevin@example.com') == 'sent to kevin@example.com'
```

`run_task` executes the function synchronously in-process and returns its
result; async task functions are bridged with `async_to_sync`, and
`arun_task` is the variant to `await` in async tests.

## Inline mode: `dbos_inline`

The `dbos_inline` fixture patches the DBOS backend so enqueued tasks execute
synchronously in-process — no system database, no worker. The enqueue still
returns a standard `TaskResult`, so you can assert on status and return value:

```python
from django.tasks import TaskResultStatus

from myapp.tasks import send_email


def test_send_email(dbos_inline):
    result = send_email.enqueue('kevin@example.com')
    assert result.status == TaskResultStatus.SUCCESSFUL
    assert result.return_value == 'sent to kevin@example.com'
```

`aenqueue()` / `aget_result()` work the same way, and a task that raises
gives a `FAILED` result carrying the exception in `result.errors` — as the
real backend (and Django's immediate backend) would — instead of propagating
it into the test. Queues are validated against the
backend's declared queues exactly as in production.

Features the inline mode cannot reproduce (deferred tasks, durable
operations) execute anyway; pass `--dbos-inline-strict` to turn them into
errors instead, so your tests don't silently diverge from production
semantics.

## Real system database: `dbos_system`

When you need real semantics (step retries, durable sleeps, schedules, crash
recovery), use `dbos_system`: the fixture launches DBOS in-process on a system
database — the current test process acts as the worker, while enqueues and
result lookups go through the same `DBOSClient` path as in production.

Tasks then run in the background, as in production: poll the result until
it finishes.

```python
import time

from django.tasks import TaskResultStatus

from myapp.tasks import send_email


def wait_until_finished(result, timeout=30):
    deadline = time.monotonic() + timeout
    while not result.is_finished:
        assert time.monotonic() < deadline, f'still {result.status} after {timeout}s'
        time.sleep(0.2)
        result.refresh()
    return result


def test_send_email_for_real(dbos_system):
    result = wait_until_finished(send_email.enqueue('kevin@example.com'))
    assert result.status == TaskResultStatus.SUCCESSFUL
    assert result.return_value == 'sent to kevin@example.com'
```

The fixture is session-scoped: DBOS is launched once, with the tasks
imported by then, and shared by every test that uses it.

With the default `--dbos-system-db=auto`, the system database is a
PostgreSQL container started through testcontainers when Docker is
available, and a SQLite file otherwise. Force one with
`--dbos-system-db=postgres` (the tests are skipped if Docker is unavailable)
or `--dbos-system-db=sqlite`. The PostgreSQL modes need the `test` extra.

## structlog

django-dbos logs through [structlog](https://www.structlog.org/) under the
`django_dbos` logger. If your tasks log through structlog too, use its
[testing utilities](https://www.structlog.org/en/stable/testing.html) to
assert on what a task logged, combined with `dbos_inline` so the task
actually runs:

```python
from structlog.testing import capture_logs

from myapp.tasks import send_email


def test_send_email_logs(dbos_inline):
    with capture_logs() as logs:
        send_email.enqueue('kevin@example.com')
    assert {'event': 'email sent', 'to': 'kevin@example.com', 'log_level': 'info'} in logs
```
