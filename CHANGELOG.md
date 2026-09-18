## [Unreleased]

### Added

- **cli**: group dbos_migrate/dbos_worker/dbos_status under a single dbos command
- ship the pytest plugin's dependencies as the django-dbos[test] extra
- add dbos_migrate and the RUN_MIGRATIONS setting
- make the capacity thresholds configurable and the advisor pluggable
- add the SERIALIZER setting for task data in the system database
- store worker heartbeats in a Django table instead of the cache
- add the EXECUTOR_ID setting to identify worker instances
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

- read __version__ via importlib.metadata
- drop every private DBOS SDK import
- build the derived postgres URL without sqlalchemy

### Fixed

- don't fail tests over background socket/thread GC warnings
- mark the examples' manage.py executable
- adapt to dbos 3.0's queue registration and graceful shutdown
- align the dbos_inline fixture with the real backend
- report Sentry cron check-ins per scheduled firing, keyed by module path
- fail fast and mask credentials when the system database is unavailable
- keep queue-less child workflows out of the per-queue capacity metrics
- report unknown queue options as a configuration error
- reconcile cron schedules with the code on worker startup
- apply the task's DBOS options to cron firings
- recycle Django database connections around each task execution
- inspect the system database through the client in admin and dbos_status
- enqueue and read results through a DBOSClient in non-worker processes
- use the real zensical config at the repo root
- clamp the workflow list page parameter
- mask credentials in config errors and validate workflow names
