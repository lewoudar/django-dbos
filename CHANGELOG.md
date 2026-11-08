## [0.1.0] - 2026-11-08

### Added

- `DBOSBackend`, a backend for Django's `django.tasks` framework: every task runs as a durable DBOS workflow that survives crashes and restarts, with configurable queues (concurrency, rate limits) and pluggable serializers.
- Access to DBOS durable primitives (steps, sleep, events, messages) from inside a task through `get_dbos_context()`.
- `@cron` scheduling of tasks, with optional Sentry cron monitors.
- A `dbos` management command: `dbos worker` to run tasks, `dbos migrate` to set up the system database (also applied by `manage.py migrate`), and `dbos status` for a worker-capacity report with pluggable advice.
- Admin pages, restricted to superusers, to browse, cancel, resume and fork workflows and to manage schedules.
- PostgreSQL, SQLite and CockroachDB support for the DBOS system database.
- A pytest plugin (`django-dbos[test]` extra) with `dbos_inline` and `dbos_system` fixtures, to run tasks inline or against a real system database.
