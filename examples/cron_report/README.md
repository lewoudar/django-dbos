# django-dbos + cron: a periodic signup report

A minimal Django project showing `@cron`: a scheduled, durable task that fires
on its own once a worker is running — no external scheduler (cron, Celery
beat) needed.

- `reports_app/models.py` — a trivial `Signup` model, just so the report has
  something to count.
- `reports_app/tasks.py` — `signup_report`, a `@cron('*/10 * * * * *')` task
  (the 6-field form: leading seconds, so it fires every 10 seconds instead of
  waiting a full minute) that logs the signup count and the most recent
  emails.
- `reports_app/management/commands/seed_signups.py` — creates a few fake
  signups so the next firing has something to report.

Everything runs on SQLite — both the Django database and (django-dbos'
default) the DBOS system database next to it — so there is nothing to
provision to try it.

## Run it

Standalone [uv](https://docs.astral.sh/uv/) project depending on the
django-dbos checkout two levels up (`tool.uv.sources` in `pyproject.toml`).
From this directory:

```bash
uv sync
uv run manage.py migrate         # Django's own tables, including the Signup model
uv run manage.py createsuperuser # optional, to browse /admin/dbos/schedules/
uv run manage.py seed_signups 3  # a few fake signups to report on
```

Start the worker — it registers the schedule with DBOS on launch and is the
only process that executes (and fires) tasks:

```bash
uv run manage.py dbos worker
```

Within ~10 seconds you should see a line like this in its output, then again
every 10 seconds:

```
signup report  latest=['4a670a97@example.com', ...]  total_signups=3
```

## Try it

While the worker is running, seed a few more signups from another terminal
(`uv run manage.py seed_signups 5`) and watch the count go up on the next
firing.

Kill the worker (Ctrl-C) and restart it: the schedule is stored in the DBOS
system database, not in the worker process, so it keeps firing on the same
cadence — nothing to re-register, no missed-schedule bookkeeping to write
yourself.

If you started a superuser above, **Home › DBOS schedules**
(http://localhost:8000/admin/dbos/schedules/ once you also run `manage.py
runserver`) lists the schedule with its crontab and last fire time, and lets
you pause, resume or delete it. **Home › DBOS workflows** shows every
individual firing as its own durable workflow execution.

See [`../ninja_api`](../ninja_api) for the enqueue/poll pattern, and the
[main documentation](https://github.com/lewoudar/django-dbos/tree/main/docs)
for `@dbos_params` options (timeouts, retries) and optional Sentry cron
monitoring.
