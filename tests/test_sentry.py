"""Unit tests for the Sentry cron monitor integration."""

import pytest

from django_dbos import sentry
from django_dbos.sentry import crontab_to_monitor_config, monitor_slug
from tests.tests_helpers import MemoryTransport, make_dbos_task


class TestMonitorSlug:
    @pytest.mark.parametrize(
        ('crontab_task_name', 'expected'),
        [
            pytest.param('cleanup', 'cleanup', id='simple'),
            pytest.param('myapp.tasks.cleanup', 'myapp-tasks-cleanup', id='dots-become-dashes'),
        ],
    )
    def test_slug(self, crontab_task_name, expected):
        assert monitor_slug(crontab_task_name) == expected

    def test_sanitizes_and_truncates(self):
        slug = monitor_slug('Weird Task!!' + 'x' * 100)
        assert len(slug) <= 50
        assert all(c in 'abcdefghijklmnopqrstuvwxyz0123456789-_' for c in slug)


class TestCrontabToMonitorConfig:
    @pytest.mark.parametrize(
        ('crontab', 'expected'),
        [
            pytest.param('*/5 * * * *', '*/5 * * * *', id='five-fields'),
            pytest.param('0 0 5 * * *', '0 5 * * *', id='six-fields-with-zero-seconds'),
            pytest.param('*/30 * * * * *', None, id='sub-minute'),
            pytest.param('* * *', None, id='invalid-field-count'),
        ],
    )
    def test_monitor_config(self, settings, crontab, expected):
        settings.TIME_ZONE = 'UTC'
        config = crontab_to_monitor_config(crontab)
        if expected is None:
            assert config is None
        else:
            assert config == {'schedule': {'type': 'crontab', 'value': expected}, 'timezone': 'UTC'}


class TestMonitorCronTask:
    def test_noop_without_sentry_sdk(self, mocker):
        mocker.patch.object(sentry, 'is_sentry_enabled', return_value=False)
        task = make_dbos_task()
        assert sentry.monitor_cron_task(task, '*/5 * * * *') is task

    def test_wraps_function_with_active_sentry(self, mocker):
        sentry_sdk = pytest.importorskip('sentry_sdk')

        transport = MemoryTransport()
        sentry_sdk.init(transport=transport)
        try:
            task = make_dbos_task()
            monitored = sentry.monitor_cron_task(task, '*/5 * * * *')
            assert monitored is not task
            assert monitored.dbos_params == task.dbos_params

            monitored.func()  # the wrapped function sends check-ins
            sentry_sdk.get_client().flush()
            assert transport.envelopes or transport.events, 'expected Sentry check-ins'
        finally:
            sentry_sdk.get_client().close()

    def test_explicit_none_disables_monitoring(self, mocker):
        mocker.patch.object(sentry, 'is_sentry_enabled', return_value=True)
        task = make_dbos_task()
        assert sentry.monitor_cron_task(task, '*/5 * * * *', sentry_monitor_config=None) is task

    def test_unrepresentable_crontab_returns_task_unchanged(self, mocker):
        pytest.importorskip('sentry_sdk')
        mocker.patch.object(sentry, 'is_sentry_enabled', return_value=True)
        task = make_dbos_task()
        assert sentry.monitor_cron_task(task, '*/30 * * * * *') is task  # sub-minute

    def test_is_sentry_enabled_without_sdk(self, mocker):
        # A missing module import raises ImportError: sys.modules[...] = None.
        mocker.patch.dict('sys.modules', {'sentry_sdk': None})
        assert sentry.is_sentry_enabled() is False

    def test_is_sentry_enabled_swallows_client_errors(self, mocker):
        sentry_sdk = pytest.importorskip('sentry_sdk')
        mocker.patch.object(sentry_sdk, 'get_client', side_effect=RuntimeError('boom'))
        assert sentry.is_sentry_enabled() is False
