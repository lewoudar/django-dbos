"""Smoke test: the package imports and exposes its version."""

import django_dbos


def test_package_exposes_version():
    assert isinstance(django_dbos.__version__, str)


def test_default_settings_module_loads(settings):
    assert settings.TASKS['default']['BACKEND'].endswith('ImmediateBackend')
