"""Unit tests for the Sentry cron monitor integration."""

from typing import ClassVar

import pytest

from django_dbos import sentry
from django_dbos.sentry import crontab_to_monitor_config, monitor_slug
from tests.helpers import MemoryTransport, make_dbos_task


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

    def test_long_paths_keep_the_function_name(self):
        slug = monitor_slug('myproject.apps.notifications.digests.tasks.send_weekly_digest_to_active_users')
        assert len(slug) == 50
        assert slug.endswith('send_weekly_digest_to_active_users')
        assert not slug.startswith('-')


class TestCrontabToMonitorConfig:
    @pytest.mark.parametrize(
        ('crontab', 'expected'),
        [
            pytest.param('*/5 * * * *', '*/5 * * * *', id='five-fields'),
            pytest.param('0 0 5 * * *', '0 5 * * *', id='six-fields-with-zero-seconds'),
            pytest.param('30 0 3 * * *', '0 3 * * *', id='six-fields-with-single-second'),
            pytest.param('00 */5 * * * *', '*/5 * * * *', id='six-fields-with-padded-zero-seconds'),
            pytest.param('*/30 * * * * *', None, id='sub-minute-step'),
            pytest.param('* * * * * *', None, id='sub-minute-wildcard'),
            pytest.param('0,30 * * * * *', None, id='sub-minute-list'),
            pytest.param('0-10 * * * * *', None, id='sub-minute-range'),
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


class TestResolveMonitorConfig:
    def test_derived_from_crontab_by_default(self, settings):
        settings.TIME_ZONE = 'UTC'
        config = sentry.resolve_monitor_config(make_dbos_task(), '*/5 * * * *')
        assert config == {'schedule': {'type': 'crontab', 'value': '*/5 * * * *'}, 'timezone': 'UTC'}

    def test_explicit_none_disables_monitoring(self):
        assert sentry.resolve_monitor_config(make_dbos_task(), '*/5 * * * *', sentry_monitor_config=None) is None

    def test_unrepresentable_crontab_is_not_monitored(self):
        assert sentry.resolve_monitor_config(make_dbos_task(), '*/30 * * * * *') is None  # sub-minute

    def test_explicit_config_is_used_as_is(self):
        custom = {'schedule': {'type': 'interval', 'value': 1, 'unit': 'hour'}, 'timezone': 'Europe/Paris'}
        assert sentry.resolve_monitor_config(make_dbos_task(), '0 * * * *', sentry_monitor_config=custom) is custom


class TestMonitorCronExecution:
    CONFIG: ClassVar[dict] = {'schedule': {'type': 'crontab', 'value': '*/5 * * * *'}, 'timezone': 'UTC'}

    def test_noop_without_sentry(self, mocker):
        mocker.patch.object(sentry, 'is_sentry_enabled', return_value=False)
        monitor = mocker.patch('sentry_sdk.crons.monitor')
        with sentry.monitor_cron_execution(make_dbos_task(), self.CONFIG):
            pass
        monitor.assert_not_called()

    def test_noop_without_monitor_config(self, mocker):
        mocker.patch.object(sentry, 'is_sentry_enabled', return_value=True)
        monitor = mocker.patch('sentry_sdk.crons.monitor')
        with sentry.monitor_cron_execution(make_dbos_task(), None):
            pass
        monitor.assert_not_called()

    def test_sends_check_ins_with_active_sentry(self):
        sentry_sdk = pytest.importorskip('sentry_sdk')

        transport = MemoryTransport()
        sentry_sdk.init(transport=transport)
        try:
            with sentry.monitor_cron_execution(make_dbos_task(), self.CONFIG):
                pass
            sentry_sdk.get_client().flush()
            assert transport.envelopes or transport.events, 'expected Sentry check-ins'
        finally:
            sentry_sdk.get_client().close()

    def test_decided_at_execution_time_not_at_import(self, mocker):
        # Sentry initialized after the task module was imported still monitors.
        enabled = mocker.patch.object(sentry, 'is_sentry_enabled', return_value=False)
        monitor = mocker.patch('sentry_sdk.crons.monitor')
        task = make_dbos_task()
        with sentry.monitor_cron_execution(task, self.CONFIG):
            pass
        monitor.assert_not_called()

        enabled.return_value = True
        with sentry.monitor_cron_execution(task, self.CONFIG):
            pass
        monitor.assert_called_once_with('tests-helpers-noop', monitor_config=self.CONFIG)

    def test_is_sentry_enabled_without_sdk(self, mocker):
        # A missing module import raises ImportError: sys.modules[...] = None.
        mocker.patch.dict('sys.modules', {'sentry_sdk': None})
        assert sentry.is_sentry_enabled() is False

    def test_is_sentry_enabled_swallows_client_errors(self, mocker):
        sentry_sdk = pytest.importorskip('sentry_sdk')
        mocker.patch.object(sentry_sdk, 'get_client', side_effect=RuntimeError('boom'))
        assert sentry.is_sentry_enabled() is False


class TestCronDecoratorLeavesTheTaskAlone:
    def test_task_function_is_not_wrapped(self, mocker):
        # The monitor lives in the scheduled parent workflow, not on the task
        # function: an ad-hoc enqueue of a cron task is not a cron execution.
        import django_dbos.scheduling as cron_module

        mocker.patch.object(sentry, 'is_sentry_enabled', return_value=True)
        task = make_dbos_task()
        assert cron_module.cron('*/5 * * * *')(task) is task
        assert cron_module._registry[task.module_path].monitor_config == {
            'schedule': {'type': 'crontab', 'value': '*/5 * * * *'},
            'timezone': 'UTC',
        }
