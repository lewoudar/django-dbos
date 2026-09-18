# django-dbos + Django Ninja: submit a task, poll its status

A minimal Django project showing the request/response pattern django-dbos is
built for: a view enqueues a durable task and returns immediately, a client
polls a status endpoint until the task is done.

- `tasks_app/tasks.py` — one `@task`, `count_words`, that sleeps for a few
  seconds so there is something to poll.
- `tasks_app/api.py` — two [Django Ninja](https://django-ninja.dev/) routes:
  `POST /api/tasks/word-count` enqueues it, `GET /api/tasks/{task_id}` reports
  its `TaskResultStatus` and, once `SUCCESSFUL`, its return value.

Everything runs on SQLite — both the Django database and (django-dbos'
default) the DBOS system database next to it — so there is nothing to
provision to try it.

## Run it

This example is a standalone [uv](https://docs.astral.sh/uv/) project that
depends on the django-dbos checkout two levels up (`tool.uv.sources` in
`pyproject.toml`). From this directory:

```bash
uv sync
uv run manage.py migrate         # Django's own tables (worker heartbeats)
uv run manage.py createsuperuser # optional, to browse /admin/dbos/workflows/
```

Then, in one terminal, start the worker — it creates the DBOS system
database on first launch and is the only process that executes tasks:

```bash
uv run manage.py dbos worker
```

In a second terminal, start the web process:

```bash
uv run manage.py runserver
```

## Try it

Submit a task:

```bash
curl -s -X POST http://localhost:8000/api/tasks/word-count \
  -H 'Content-Type: application/json' \
  -d '{"text": "durable tasks that survive a worker crash"}'
# {"task_id": "...", "status": "READY"}
```

Poll it with the `task_id` from the response above — status moves through
`READY` / `RUNNING` to `SUCCESSFUL` over the 5 second sleep in `count_words`:

```bash
curl -s http://localhost:8000/api/tasks/<task_id>
# {"task_id": "...", "status": "RUNNING", "result": null}
# ... a few seconds later ...
# {"task_id": "...", "status": "SUCCESSFUL", "result": {"word_count": 7, "char_count": 41}}
```

Kill the `dbos worker` process mid-sleep and restart it: the task resumes
rather than losing the enqueue, which is the whole point of django-dbos over
a plain in-memory task queue.

Interactive API docs are at http://localhost:8000/api/docs. The DBOS admin
pages (workflow detail, step history, cancel/resume/fork) are at
http://localhost:8000/admin/dbos/workflows/, for the superuser created above.

See the [main documentation](https://github.com/lewoudar/django_dbos/tree/main/docs)
for configuration options, cron scheduling and deployment.
