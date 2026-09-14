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
    @pytest.mark.parametrize(
        ('crontab', 'expected'),
        [
            pytest.param('*/5 * * * *', '0 */5 * * * *', id='five-fields-get-zero-seconds'),
            pytest.param('0 */5 * * * *', '0 */5 * * * *', id='six-fields-unchanged'),
            pytest.param('30 4 1,15 * *', '0 30 4 1,15 * *', id='five-fields-with-dom-list'),
        ],
    )
    def test_normalized(self, crontab, expected):
        assert normalize_crontab(crontab) == expected

    def test_invalid_field_count(self):
        with pytest.raises(ImproperlyConfiguredDbos):
            normalize_crontab('* * *')


class TestCronDecorator:
    def test_invalid_crontab_rejected(self):
        with pytest.raises(ImproperlyConfiguredDbos):
            cron_module.cron('not a crontab')

    def test_required_args_rejected_at_decoration(self):
        with pytest.raises(ImproperlyConfiguredDbos):
            importlib.import_module('tests.tasks_invalid_cron')

    def test_valid_cron_task_registers(self):
        # The registry is cleared between tests; re-apply the decorator to a
        # module-level task.
        cron_module.cron('*/5 * * * *')(cron_task)

        assert cron_task.module_path in cron_module._registry
        registration = cron_module._registry[cron_task.module_path]
        assert registration.crontab == '0 */5 * * * *'
        assert schedule_name_for_task(cron_task) == (f'django_dbos.schedules.{cron_task.module_path}')


class TestDjangoTimezone:
    def test_uses_default_timezone_when_use_tz(self, settings):
        settings.USE_TZ = True
        from django.utils import timezone as dj_timezone

        assert cron_module._django_timezone() == dj_timezone.get_default_timezone()

    def test_falls_back_to_utc_without_use_tz(self, settings):
        settings.USE_TZ = False
        assert cron_module._django_timezone() == 'UTC'


class TestRegisterCronSchedules:
    @pytest.mark.integration
    def test_updates_an_existing_schedule_with_a_changed_crontab(self, dbos_environment):
        from dbos import DBOS

        from django_dbos.scheduling import _register_scheduled_workflow

        name = schedule_name_for_task(cron_task)
        if DBOS.get_schedule(name) is not None:  # may exist from other tests
            DBOS.delete_schedule(name)
        scheduled_workflow = _register_scheduled_workflow(cron_task)
        DBOS.create_schedule(
            schedule_name=name,
            workflow_fn=scheduled_workflow,
            schedule='0 0 1 1 *',  # different from the task's crontab
            queue_name='default',
        )

        cron_module.cron('*/5 * * * *')(cron_task)
        cron_module.register_cron_schedules()

        schedule = DBOS.get_schedule(name)
        assert schedule is not None
        assert schedule['schedule'] == '0 */5 * * * *'
