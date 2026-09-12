"""Unit tests for the Sentry cron monitor integration."""

import pytest

from django_dbos import sentry
from django_dbos.sentry import crontab_to_monitor_config, monitor_slug
from tests.tests_helpers import MemoryTransport, make_dbos_task


class TestMonitorSlug:
    def test_simple(self):
        assert monitor_slug("cleanup") == "cleanup"

    def test_dots_become_dashes(self):
        assert monitor_slug("myapp.tasks.cleanup") == "myapp-tasks-cleanup"

    def test_sanitizes_and_truncates(self):
        slug = monitor_slug("Weird Task!!" + "x" * 100)
        assert len(slug) <= 50
        assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in slug)


class TestCrontabToMonitorConfig:
    def test_five_field_crontab(self, settings):
        settings.TIME_ZONE = "UTC"
        config = crontab_to_monitor_config("*/5 * * * *")
        assert config == {
            "schedule": {"type": "crontab", "value": "*/5 * * * *"},
            "timezone": "UTC",
        }

    def test_six_field_crontab_with_zero_seconds(self):
        config = crontab_to_monitor_config("0 0 5 * * *")
        assert config is not None
        assert config["schedule"] == {"type": "crontab", "value": "0 5 * * *"}

    def test_sub_minute_schedule_returns_none(self):
        assert crontab_to_monitor_config("*/30 * * * * *") is None

    def test_invalid_field_count_returns_none(self):
        assert crontab_to_monitor_config("* * *") is None


class TestMonitorCronTask:
    def test_noop_without_sentry_sdk(self, mocker):
        mocker.patch.object(sentry, "is_sentry_enabled", return_value=False)
        task = make_dbos_task()
        assert sentry.monitor_cron_task(task, "*/5 * * * *") is task

    def test_wraps_function_with_active_sentry(self, mocker):
        sentry_sdk = pytest.importorskip("sentry_sdk")

        transport = MemoryTransport()
        sentry_sdk.init(transport=transport)
        try:
            task = make_dbos_task()
            monitored = sentry.monitor_cron_task(task, "*/5 * * * *")
            assert monitored is not task
            assert monitored.dbos_params == task.dbos_params

            monitored.func()  # the wrapped function sends check-ins
            sentry_sdk.get_client().flush()
            assert transport.envelopes or transport.events, "expected Sentry check-ins"
        finally:
            sentry_sdk.get_client().close()

    def test_explicit_none_disables_monitoring(self, mocker):
        mocker.patch.object(sentry, "is_sentry_enabled", return_value=True)
        task = make_dbos_task()
        assert sentry.monitor_cron_task(task, "*/5 * * * *", sentry_monitor_config=None) is task
