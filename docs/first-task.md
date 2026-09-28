# Your first task

This walkthrough takes a fresh Django project from zero to a task running in
the background, then shows what "durable" means in practice by crashing the
worker on purpose. It assumes you followed [Installation](index.md#installation)
on a project using Django's default SQLite database: django-dbos then creates
its system database next to it (`db.sqlite3-dbos.sqlite`), so there is
nothing else to set up.

## 1. Write a task

Tasks live in a `tasks.py` module of one of your installed apps (say
`emails`). That name matters: the worker imports the `tasks` (or
`dbos_tasks`) module of every installed app to find what it can run.

```python
# emails/tasks.py
from django.tasks import task


@task
def count_words(text: str) -> int:
    return len(text.split())
```

A task is a plain module-level function with Django's `@task` decorator —
nothing specific to django-dbos yet.

## 2. Start a worker

Tasks are not executed by your web server but by a separate, long-running
process. Start it in a first terminal and leave it running:

```bash
python manage.py dbos worker
```

```
...
DBOS worker started. Press Ctrl+C to stop.
```

!!! note
    The worker does not reload on code changes like `runserver` does:
    restart it after editing a task.

## 3. Enqueue the task

In a second terminal, open a Django shell. The shell plays the part of your
web application here: it enqueues, it never executes.

```pycon
>>> from emails.tasks import count_words
>>> result = count_words.enqueue('hello durable world')
>>> result.status
TaskResultStatus.READY
```

`enqueue()` returns immediately with a `TaskResult`: the task is recorded in
the system database, waiting for a worker (`READY`). While a worker executes
it, its status is `RUNNING`. A moment later:

```pycon
>>> result.refresh()
>>> result.status
TaskResultStatus.SUCCESSFUL
>>> result.return_value
3
```

`result.id` identifies this execution. Store it (in a model, a session, an
API response...) to look the result up later, from any process:

```pycon
>>> count_words.get_result(result.id).return_value
3
```

## 4. From a view

The same two calls, in the shape a web application usually needs — one view
starts the work, another reports on it:

```python
# emails/views.py
from django.http import JsonResponse
from django.tasks import TaskResultStatus
from django.tasks.exceptions import TaskResultDoesNotExist
from django.views.decorators.http import require_POST

from .tasks import count_words


@require_POST
def start_count(request):
    result = count_words.enqueue(request.POST['text'])
    return JsonResponse({'id': result.id}, status=202)


def count_status(request, result_id):
    try:
        result = count_words.get_result(result_id)
    except TaskResultDoesNotExist:
        return JsonResponse({'error': 'unknown task'}, status=404)
    data = {'status': result.status}
    if result.status == TaskResultStatus.SUCCESSFUL:
        data['words'] = result.return_value
    return JsonResponse(data)
```

The client posts some text, gets an `id` back right away, and polls
`count_status` until the status is `SUCCESSFUL` (or `FAILED`).
[`examples/ninja_api`](https://github.com/lewoudar/django-dbos/tree/main/examples/ninja_api)
is a complete, runnable version of this pattern.

## 5. Look at it in the admin

Every execution is visible in the Django admin, with its inputs, output and
error. The pages are reserved to superusers:

```bash
python manage.py createsuperuser
python manage.py runserver
```

Then open <http://127.0.0.1:8000/admin/dbos/workflows/>. See [Admin](admin.md)
for what you can do from there (cancel, resume, re-run...).

## 6. See durability at work

**Without a worker**, enqueuing still works: stop the worker (`Ctrl+C`),
enqueue a task from the shell, and its status stays `READY`. Start the worker
again and it picks the task up — nothing was lost while no worker was
running.

**When a worker crashes** mid-task, the task is not lost either. Add a slow
task, restart the worker so it knows about it, and enqueue it:

```python
# emails/tasks.py
import time


@task
def slow_report(name: str) -> str:
    print(f'starting report {name}')
    time.sleep(10)
    print(f'finished report {name}')
    return f'report {name}'
```

As soon as the worker prints `starting report ...`, kill it hard (close the
terminal, or `kill -9` its pid) — a clean `Ctrl+C` would let the task finish
first. Start the worker again:

```
Recovering 1 workflows from application version ...
starting report march
finished report march
```

The task ran again and completed; its result is `SUCCESSFUL`.

Notice that it restarted **from the beginning**: by default a task is a
single unit of work. That is fine for `slow_report`, but not for a task that
sends 10,000 emails one by one. For those, [Durable workflows](tasks.md#durable-workflows)
let a task checkpoint its progress, so a crash resumes it where it stopped
rather than from the top.

## Next steps

- [Tasks](tasks.md): deferred execution, priorities, retries, timeouts.
- [Configuration](configuration.md): using PostgreSQL, declaring queues and
  their concurrency.
- [Cron scheduling](cron.md): run a task on a schedule.
- [Testing](testing.md): run tasks synchronously in your test suite.
- [Deployment](deployment.md): what to settle before going to production.
- [Troubleshooting](troubleshooting.md): a task stays `READY`, an error you
  don't understand.
