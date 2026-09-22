"""Tests for the @cron decorator and DBOS schedule creation."""

from __future__ import annotations

import importlib

import pytest
from dbos import DBOS
from django.utils import timezone as dj_timezone

import django_dbos.scheduling as cron_module
from django_dbos.exceptions import ImproperlyConfiguredDbos
from django_dbos.scheduling import _register_scheduled_workflow, normalize_crontab, schedule_name_for_task
from tests.tasks import cron_task, durable_context_task


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

    def test_context_and_durable_operations_task_registers(self):
        # takes_context's injected first arg is skipped regardless of
        # durable_operations; the function otherwise takes no arguments.
        cron_module.cron('*/5 * * * *')(durable_context_task)

        assert durable_context_task.module_path in cron_module._registry


class TestDjangoTimezone:
    def test_uses_default_timezone_when_use_tz(self, settings):
        settings.USE_TZ = True
        assert cron_module._django_timezone() == dj_timezone.get_default_timezone()

    def test_falls_back_to_utc_without_use_tz(self, settings):
        settings.USE_TZ = False
        assert cron_module._django_timezone() == 'UTC'


@pytest.mark.integration
class TestRegisterCronSchedules:
    @pytest.fixture
    def stale_schedule(self, dbos_environment):
        """Create the cron_task schedule with values that differ from the code, return a factory."""
        name = schedule_name_for_task(cron_task)
        scheduled_workflow = _register_scheduled_workflow(cron_task)

        def create(**overrides):
            if DBOS.get_schedule(name) is not None:  # may exist from other tests
                DBOS.delete_schedule(name)
            values = {'schedule': '0 */5 * * * *', 'cron_timezone': 'UTC', 'queue_name': 'default', **overrides}
            DBOS.create_schedule(schedule_name=name, workflow_fn=scheduled_workflow, **values)
            return name

        return create

    @pytest.mark.parametrize(
        'stale',
        [
            pytest.param({'schedule': '0 0 1 1 *'}, id='crontab'),
            pytest.param({'cron_timezone': 'Europe/Paris'}, id='timezone'),
            pytest.param({'queue_name': 'emails'}, id='queue'),
        ],
    )
    def test_recreates_a_schedule_whose_definition_changed(self, stale_schedule, stale):
        name = stale_schedule(**stale)

        cron_module.cron('*/5 * * * *')(cron_task)
        cron_module.register_cron_schedules()

        schedule = DBOS.get_schedule(name)
        assert schedule is not None
        assert schedule['schedule'] == '0 */5 * * * *'
        assert schedule['cron_timezone'] == 'UTC'
        assert schedule['queue_name'] == 'default'

    def test_leaves_an_up_to_date_schedule_alone(self, stale_schedule, mocker):
        stale_schedule()
        delete = mocker.spy(cron_module.DBOS, 'delete_schedule')

        cron_module.cron('*/5 * * * *')(cron_task)
        cron_module.register_cron_schedules()

        delete.assert_not_called()

    def test_deletes_orphan_schedules(self, stale_schedule):
        name = stale_schedule()  # cron_task is scheduled in the database...
        cron_module.register_cron_schedules()  # ...but no task carries @cron in this process

        assert DBOS.get_schedule(name) is None

    def test_ignores_schedules_of_other_applications(self, dbos_environment, mocker):
        foreign = 'someone_else.nightly'
        if DBOS.get_schedule(foreign) is None:
            DBOS.create_schedule(
                schedule_name=foreign,
                workflow_fn=_register_scheduled_workflow(cron_task),
                schedule='0 0 1 1 *',
                queue_name='default',
            )
        try:
            cron_module.register_cron_schedules()
            assert DBOS.get_schedule(foreign) is not None
        finally:
            DBOS.delete_schedule(foreign)
