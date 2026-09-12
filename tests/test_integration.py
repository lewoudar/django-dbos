"""Integration tests against a real DBOS system database (Postgres)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.tasks import TaskResultStatus
from django.utils import timezone

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


def test_sync_task_roundtrip(dbos_environment):
    result = add.enqueue(3, 4)
    result = wait_for_result(result, TaskResultStatus.SUCCESSFUL)
    assert result.return_value == 7
    assert result.args == [3, 4]


def test_async_task_roundtrip(dbos_environment):
    result = async_add.enqueue(10, 20)
    result = wait_for_result(result, TaskResultStatus.SUCCESSFUL)
    assert result.return_value == 30


def test_failed_task_reports_error(dbos_environment):
    result = failing_task.enqueue()
    result = wait_for_result(result, TaskResultStatus.FAILED)
    assert result.errors
    assert result.errors[0].exception_class is ValueError


def test_task_on_declared_queue(dbos_environment):
    result = send_email.enqueue("kevin@example.com")
    result = wait_for_result(result, TaskResultStatus.SUCCESSFUL)
    assert result.return_value == "sent to kevin@example.com"


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
    from . import tasks as tasks_module

    tasks_module.results["events"] = []
    result = durable_onboarding.enqueue()
    result = wait_for_result(result, TaskResultStatus.SUCCESSFUL)
    assert result.return_value == ["step1", "step2"]
