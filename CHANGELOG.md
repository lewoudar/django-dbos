## [Unreleased]

### Added

- confirmation page for schedule deletion
- dbos_status command and worker heartbeats for capacity planning
- add dbos_status command and worker heartbeats for capacity planning
- restrict django-dbos admin pages to superusers
- admin pages for DBOS workflows and schedules
- add admin pages for DBOS workflows and schedules
- pytest plugin with inline and real-system testing modes
- add pytest plugin with dbos_inline and dbos_system fixtures
- DBOS backend for django.tasks with cron scheduling and Sentry monitoring
- add test helpers, documentation site and CI hardening
- add @cron scheduling with optional Sentry cron monitors
- expose DBOS durable primitives via get_dbos_context()
- add DBOSBackend for django.tasks with durable task execution
- resolve DJANGO_DBOS settings and manage the DBOS singleton

### Changed

- build the derived postgres URL without sqlalchemy

### Fixed

- use the real zensical config at the repo root
- clamp the workflow list page parameter
- mask credentials in config errors and validate workflow names
