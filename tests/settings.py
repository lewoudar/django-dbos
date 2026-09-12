"""Minimal Django settings used by the django-dbos test suite."""

SECRET_KEY = "django-dbos-test-secret-key"  # noqa: S105 - test-only

DEBUG = True

ROOT_URLCONF = "tests.urls"

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.sessions",
    "django.contrib.messages",
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
    # Explicit for clarity: the admin pages are registered by default and
    # restricted to superusers.
    "ADMIN_ENABLED": True,
    "SCHEDULER_POLLING_INTERVAL_SEC": 0.5,
    "QUEUES": {
        "default": None,
        "emails": None,
    },
}
