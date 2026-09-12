# Testing

## Running task functions directly

The simplest way to test a task body is to bypass the backend entirely:

```python
from django_dbos.test import run_task, arun_task

def test_send_email():
    result = run_task(send_email, "kevin@example.com")
    assert result == "sent to kevin@example.com"
```

`run_task` executes the function synchronously in-process (async task functions
are bridged with `async_to_sync`).

## Using another backend

If your tasks must go through the tasks API, point them at Django's immediate
or dummy backend in tests:

```python
from django.test import override_settings

@override_settings(TASKS={"default": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"}})
def test_something(): ...
```

## structlog

django-dbos logs through [structlog](https://www.structlog.org/) under the
`django_dbos` logger. Use structlog's
[testing utilities](https://www.structlog.org/en/stable/testing.html) to assert
on log output:

```python
from structlog.testing import capture_logs

def test_enqueue_logs(capsys):
    with capture_logs() as logs:
        send_email.enqueue("kevin@example.com")
    assert any(event == "enqueued task" for event in [log["event"] for log in logs])
```

## Testing scheduled tasks

`@cron` only registers the schedule; test the task body itself with
`run_task`. For end-to-end schedule testing, see the integration suite of
django-dbos itself (tests/test_integration.py), which runs against a live DBOS
system database.
