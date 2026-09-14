"""Integration tests against a real DBOS system database (Postgres)."""

from __future__ import annotations

import time
from datetime import timedelta

import pytest
from dbos import DBOS
from django.tasks import TaskResultStatus
from django.utils import timezone

from django_dbos.scheduling import register_cron_schedules

from . import tasks as tasks_module
from .conftest import wait_for_result
from .tasks import (
    add,
    async_add,
    durable_onboarding,
    failing_task,
    flaky_calls,
    flaky_with_retries,
    send_email,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ('task', 'args', 'expected'),
    [
        pytest.param(add, (3, 4), 7, id='sync'),
        pytest.param(async_add, (10, 20), 30, id='async'),
    ],
)
def test_task_roundtrip(dbos_environment, task, args, expected):
    result = task.enqueue(*args)
    result = wait_for_result(result, TaskResultStatus.SUCCESSFUL)
    assert result.return_value == expected
    assert result.args == list(args)


def test_failed_task_reports_error(dbos_environment):
    result = failing_task.enqueue()
    result = wait_for_result(result, TaskResultStatus.FAILED)
    assert result.errors
    assert result.errors[0].exception_class is ValueError


def test_task_on_declared_queue(dbos_environment):
    result = send_email.enqueue('kevin@example.com')
    result = wait_for_result(result, TaskResultStatus.SUCCESSFUL)
    assert result.return_value == 'sent to kevin@example.com'


def test_deferred_task(dbos_environment):
    deferred = add.using(run_after=timezone.now() + timedelta(seconds=1))
    result = deferred.enqueue(1, 2)
    result = wait_for_result(result, TaskResultStatus.SUCCESSFUL)
    assert result.return_value == 3


def test_step_retry_after_transient_failure(dbos_environment):
    flaky_calls.clear()
    result = flaky_with_retries.enqueue()
    result = wait_for_result(result, TaskResultStatus.SUCCESSFUL)
    assert result.return_value == 2
    # The failing first attempt was checkpointed and retried by DBOS.
    assert len(flaky_calls) == 2


def test_durable_operations_in_task(dbos_environment):
    tasks_module.results['events'] = []
    result = durable_onboarding.enqueue()
    result = wait_for_result(result, TaskResultStatus.SUCCESSFUL)
    assert result.return_value == ['step1', 'step2']


def test_cron_schedule_created_and_fires(dbos_environment):
    tasks_module.cron_runs.clear()
    register_cron_schedules()

    schedule_name = f'django_dbos.schedules.{tasks_module.scheduled_cleanup.module_path}'
    schedule = DBOS.get_schedule(schedule_name)
    assert schedule is not None
    assert schedule['schedule'] == '* * * * * *'

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not tasks_module.cron_runs:
        time.sleep(0.5)
    assert tasks_module.cron_runs, 'cron task did not fire within 15s'
