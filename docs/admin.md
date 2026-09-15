# Admin

django-dbos registers custom pages in the Django admin to inspect and operate
on DBOS workflows — no models, no migrations: the pages read the DBOS system
database through the DBOS Python API.

!!! note
    Access requires the Django admin (`django.contrib.admin` in
    `INSTALLED_APPS`); the pages are **superuser-only** — a plain staff
    account gets a 403, because they expose every workflow's data
    (inputs, outputs, errors) and destructive operations.

## Workflows

Open **Home › DBOS workflows** (`/admin/dbos/workflows/`):

- list with filters (status, queue, workflow name), ID prefix search and
  pagination, newest first;
- detail page with identity fields, pretty-printed input/output/error and the
  full step list (`list_workflow_steps`): duration, output, error, and links
  to child workflows;
- operational actions on the detail page and their own POST endpoints:

| Action | Effect |
| --- | --- |
| Cancel | Marks the workflow CANCELLED (in-flight step is interrupted). |
| Resume | Resumes a cancelled workflow. |
| Fork | Runs a copy of the workflow from step 0 and redirects to it. |
| Delete | Permanently removes the workflow and its history (with a confirmation page). |

## Schedules

**Home › DBOS schedules** (`/admin/dbos/schedules/`) lists every DBOS schedule
(registered by `@cron` tasks when a worker starts) with its crontab, timezone,
queue and last fire time. Each schedule can be paused, resumed or deleted
(deletion asks for confirmation first, like workflow deletion).

## Options

The pages are enabled by default; you can disable them entirely:

```python
DJANGO_DBOS = {
    # Disable the admin pages entirely:
    'ADMIN_ENABLED': False,
    # Register the pages on custom AdminSite instances (dotted paths):
    'ADMIN_SITE': ['myapp.admin.admin_site'],
}
```

`ADMIN_SITE` defaults to `["django.contrib.admin.site"]`. If
`django.contrib.admin` is not installed at all, registration is skipped.

!!! warning
    The DBOS pages let any superuser cancel, fork and permanently delete
    workflows and schedules, and display workflow inputs and outputs, which
    may contain secrets or personal data. Any plain staff account is
    rejected (403). On a deployment where superusers are not fully trusted,
    set `ADMIN_ENABLED` to `False`.

## Worker heartbeats

**Home › Django DBOS › Worker heartbeats** lists every worker process that
reported recently: executor id, host, pid, served queues, CPU/RSS (with the
`[metrics]` extra) and last-seen time. Rows are written by the workers
themselves (see [Worker capacity](status.md)); the page is read-only and
superuser-only, like the DBOS pages.
