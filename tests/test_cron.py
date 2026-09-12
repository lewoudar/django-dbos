"""Tests for the @cron decorator and DBOS schedule creation."""

from __future__ import annotations

import importlib

import pytest

import django_dbos.scheduling as cron_module
from django_dbos.exceptions import ImproperlyConfiguredDbos
from django_dbos.scheduling import normalize_crontab, schedule_name_for_task
from tests.tasks import cron_task


@pytest.fixture(autouse=True)
def _clean_cron_registry():
    saved = dict(cron_module._registry)
    cron_module._registry.clear()
    yield
    cron_module._registry.clear()
    cron_module._registry.update(saved)


class TestNormalizeCrontab:
    def test_five_fields_get_zero_seconds(self):
        assert normalize_crontab("*/5 * * * *") == "0 */5 * * * *"

    def test_six_fields_unchanged(self):
        assert normalize_crontab("0 */5 * * * *") == "0 */5 * * * *"

    def test_invalid_field_count(self):
        with pytest.raises(ImproperlyConfiguredDbos):
            normalize_crontab("* * *")


class TestCronDecorator:
    def test_invalid_crontab_rejected(self):
        with pytest.raises(ImproperlyConfiguredDbos):
            cron_module.cron("not a crontab")

    def test_required_args_rejected_at_decoration(self):
        with pytest.raises(ImproperlyConfiguredDbos):
            importlib.import_module("tests.tasks_invalid_cron")

    def test_valid_cron_task_registers(self):
        # The registry is cleared between tests; re-apply the decorator to a
        # module-level task.
        cron_module.cron("*/5 * * * *")(cron_task)

        assert cron_task.module_path in cron_module._registry
        registration = cron_module._registry[cron_task.module_path]
        assert registration.crontab == "0 */5 * * * *"
        assert schedule_name_for_task(cron_task) == (f"django_dbos.schedules.{cron_task.module_path}")
