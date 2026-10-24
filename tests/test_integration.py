"""Integration tests against a real DBOS system database (Postgres)."""

from __future__ import annotations

import time
from datetime import timedelta

import pytest
from dbos import DBOS
from dbos import error as dbos_error
from django.tasks import TaskResultStatus
from django.utils import timezone

from django_dbos.client import get_client
from django_dbos.context import NoActiveDbosContext
from django_dbos.exceptions import TaskCancelled, TaskTimedOut
from django_dbos.scheduling import register_cron_schedules
from django_dbos.workflow_names import workflow_name_for_task

from . import tasks as tasks_module
from .conftest import wait_for_result
from .tasks import (
    add,
    async_add,
    async_streaming_task,
    context_without_durable_operations,
    durable_context_task,
    durable_onboarding,
    durable_step_with_retries,
    export_task,
    failing_task,
    flaky_calls,
    flaky_with_retries,
    send_email,
    slow_cron_task,
    slow_task,
    streaming_task,
    sync_user,
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


def test_callable_deduplication_id_rejects_duplicates_per_argument(dbos_environment):
    # Deferred so the first execution of each user is still enqueued during the test.
    deferred = sync_user.using(run_after=timezone.now() + timedelta(minutes=1))
    first = deferred.enqueue(1)
    other_user = deferred.enqueue(2)

    with pytest.raises(dbos_error.DBOSQueueDeduplicatedError):
        deferred.enqueue(1)

    client = get_client()
    assert client.retrieve_workflow(first.id).get_status().deduplication_id == 'sync-user-1'
    assert client.retrieve_workflow(other_user.id).get_status().deduplication_id == 'sync-user-2'
    for result in (first, other_user):
        client.cancel_workflow(result.id)


def test_step_retry_after_transient_failure(dbos_environment):
    flaky_calls.clear()
    result = flaky_with_retries.enqueue()
    result = wait_for_result(result, TaskResultStatus.SUCCESSFUL)
    assert result.return_value == 2
    # The failing first attempt was checkpointed and retried by DBOS.
    assert len(flaky_calls) == 2


def test_run_step_retries_with_step_options(dbos_environment):
    flaky_calls.clear()
    result = durable_step_with_retries.enqueue()
    result = wait_for_result(result, TaskResultStatus.SUCCESSFUL)
    assert result.return_value == 2
    assert len(flaky_calls) == 2


@pytest.mark.parametrize('task', [streaming_task, async_streaming_task], ids=['sync', 'async'])
def test_streams_and_events_from_a_durable_task(dbos_environment, task):
    result = task.enqueue(3)
    # Read from outside the worker, as a view would.
    values = list(get_client().read_stream(result.id, 'progress', timeout_seconds=30))
    assert values == [0, 1, 2]

    result = wait_for_result(result, TaskResultStatus.SUCCESSFUL)
    assert result.return_value == {'first': 1, 'second': 2}


def test_failed_durable_task_is_forked_from_its_failing_step(dbos_environment):
    tasks_module.export_calls.clear()
    tasks_module.export_failures.add(2)
    try:
        result = wait_for_result(export_task.enqueue(), TaskResultStatus.FAILED)
    finally:
        tasks_module.export_failures.clear()
    client = get_client()
    [failed_step] = [step for step in client.list_workflow_steps(result.id) if step['error'] is not None]

    tasks_module.export_calls.clear()
    handle = client.fork_workflow(result.id, failed_step['function_id'])

    # The fork is a task result like any other, and reuses the steps before the failing one.
    forked = wait_for_result(export_task.get_result(handle.workflow_id), TaskResultStatus.SUCCESSFUL)
    assert forked.return_value == 4
    assert tasks_module.export_calls == ['render-2', 'render-3', 'render-4']
    # The stream values written before the fork point are copied.
    lines = list(client.read_stream(forked.id, 'lines', timeout_seconds=5))
    assert lines == ['order 1', 'order 2', 'order 3', 'order 4']


def test_durable_operations_in_task(dbos_environment):
    tasks_module.results['events'] = []
    result = durable_onboarding.enqueue()
    result = wait_for_result(result, TaskResultStatus.SUCCESSFUL)
    assert result.return_value == ['step1', 'step2']


def test_dbos_context_requires_durable_operations(dbos_environment):
    result = context_without_durable_operations.enqueue()
    result = wait_for_result(result, TaskResultStatus.FAILED)
    assert result.errors[0].exception_class is NoActiveDbosContext


def test_context_and_durable_operations_combine(dbos_environment):
    tasks_module.results['events'] = []
    result = durable_context_task.enqueue()
    result = wait_for_result(result, TaskResultStatus.SUCCESSFUL)
    assert result.return_value == {'attempt': 1, 'events': ['durable-context-step']}


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


def test_workflow_timeout_cancels_an_enqueued_task(dbos_environment):
    result = wait_for_result(slow_task.enqueue(), TaskResultStatus.FAILED, timeout=15)
    status = DBOS.get_workflow_status(result.id)
    assert status is not None
    assert str(status.status) == 'CANCELLED'
    assert result.errors[0].exception_class is TaskTimedOut


def test_cancelled_task_reports_task_cancelled(dbos_environment):
    # Deferred, so the cancellation lands before any worker starts it.
    result = add.using(run_after=timezone.now() + timedelta(seconds=30)).enqueue(1, 2)
    get_client().cancel_workflow(result.id)

    result = wait_for_result(result, TaskResultStatus.FAILED)
    assert result.errors[0].exception_class is TaskCancelled


def test_workflow_timeout_applies_to_cron_firings(dbos_environment):
    register_cron_schedules()
    # Each firing runs the task's regular workflow as a child: the timeout
    # cancels that child, under the same name enqueued executions use.
    child_name = workflow_name_for_task(slow_cron_task)

    deadline = time.monotonic() + 25
    cancelled: list = []
    while time.monotonic() < deadline and not cancelled:
        cancelled = DBOS.list_workflows(name=child_name, status='CANCELLED', load_input=False, load_output=False)
        time.sleep(0.5)
    assert cancelled, 'no cron firing was cancelled by its workflow_timeout within 25s'
    assert all(status.schedule_name is None for status in cancelled)  # the child, not the schedule's parent
