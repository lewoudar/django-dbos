"""Minimal Django settings used by the django-dbos test suite."""

SECRET_KEY = "django-dbos-test-secret-key"  # noqa: S105 - test-only

DEBUG = True

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django_dbos",
    "tests",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    },
}

USE_TZ = True

TIME_ZONE = "UTC"

# Use Django's immediate backend by default; the "dbos" backend is exercised
# by integration tests (marker "integration", requires Docker for Postgres).
TASKS = {
    "default": {
        "BACKEND": "django.tasks.backends.immediate.ImmediateBackend",
        "QUEUES": ["default"],
    },
    "dbos": {
        "BACKEND": "django_dbos.backends.DBOSBackend",
        "QUEUES": ["default", "emails"],
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Do not spin up a DBOS singleton on app ready in the test suite; tests that
# need it call dbos_setup helpers explicitly.
DJANGO_DBOS = {
    "AUTO_INIT": False,
    "SCHEDULER_POLLING_INTERVAL_SEC": 0.5,
    "QUEUES": {
        "default": None,
        "emails": None,
    },
}
