"""Minimal Django settings used by the django-dbos test suite."""

SECRET_KEY = "django-dbos-test-secret-key"

DEBUG = True

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django_dbos",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    },
}

USE_TZ = True

TIME_ZONE = "UTC"

# Use Django's immediate backend by default; integration tests override this
# with the DBOS backend.
TASKS = {
    "default": {
        "BACKEND": "django.tasks.backends.immediate.ImmediateBackend",
        "QUEUES": ["default"],
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Do not spin up a DBOS singleton on app ready in the test suite; tests that
# need it call dbos_setup helpers explicitly.
DJANGO_DBOS = {"AUTO_INIT": False}
