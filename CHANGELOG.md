# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `DBOSBackend`, a DBOS-backed backend for Django's tasks framework
  (`django.tasks`): durable task execution on database-backed queues, with
  support for async tasks, deferred tasks (`run_after`), priorities and result
  retrieval.
- `DBOSTask` with DBOS execution options attached via `@dbos_params` (step
  retries, deduplication, queue partition key, workflow timeout, recovery
  attempts).
- `get_dbos_context()` giving access to DBOS durable primitives (checkpointed
  steps, durable sleep, events, durable messages) inside tasks declared with
  `durable_operations=True`.
- `@cron` decorator stacking on `@task` to schedule tasks with crontab
  expressions, backed by idempotent DBOS schedules and managed by
  `manage.py dbos_worker`.
- Optional `sentry` extra: Sentry cron monitors for scheduled tasks, with
  monitor configuration derived from the crontab and per-task opt-out.
- System database URL derived from the default Django connection (PostgreSQL
  or SQLite), overridable via `DJANGO_DBOS["SYSTEM_DATABASE_URL"]` or
  `DBOS_SYSTEM_DATABASE_URL`.
- Queue declaration and configuration from `DJANGO_DBOS["QUEUES"]`.
- `django_dbos.test.run_task` / `arun_task` helpers to execute task functions
  in tests without a system database.
- Pytest plugin (loaded automatically) with `dbos_inline` — synchronous
  in-process execution without a system database, `--dbos-inline-strict` to
  fail on unreproducible semantics — and `dbos_system` — a real in-process
  DBOS worker on a SQLite file or a PostgreSQL container
  (`--dbos-system-db=postgres|auto|sqlite`).
- structlog-based logging under the `django_dbos` logger.
- System checks validating the configuration.
- Admin pages (staff-only, registered on the configured AdminSite instances):
  workflow list with filters and pagination, detail view with steps
  (durations, outputs, child workflow links), schedule list, and operational
  actions (cancel, resume, fork, delete workflows; pause, resume, delete
  schedules). Gated by `DJANGO_DBOS["ADMIN_ENABLED"]` / `ADMIN_SITE`.
