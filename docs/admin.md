# Admin

django-dbos registers custom pages in the Django admin to inspect and operate
on DBOS workflows — no models, no migrations: the pages read the DBOS system
database through the DBOS Python API.

!!! note
    Access requires the Django admin (`django.contrib.admin` in
    `INSTALLED_APPS`); all pages are staff-only.

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
queue and last fire time. Each schedule can be paused, resumed or deleted.

## Options

```python
DJANGO_DBOS = {
    # Disable the admin pages entirely:
    "ADMIN_ENABLED": False,
    # Register the pages on custom AdminSite instances (dotted paths):
    "ADMIN_SITE": ["myapp.admin.admin_site"],
}
```

`ADMIN_SITE` defaults to `["django.contrib.admin.site"]`. If
`django.contrib.admin` is not installed at all, registration is skipped.

!!! note
    The pages require the DBOS singleton to be launched: opening them runs
    `ensure_launched()`, which starts DBOS' background threads in the web
    process. In a split deployment (web + workers), consider pointing the
    admin at a deployment where this is acceptable, or serve it read-only
    behind your usual access controls.
