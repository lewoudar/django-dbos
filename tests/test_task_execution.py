"""Tests for the task body execution helpers (connection hygiene, context)."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from unittest.mock import AsyncMock, MagicMock

import pytest
from dbos import DBOS
from django.tasks import TaskContext, TaskResultStatus, task
from structlog.testing import capture_logs

from django_dbos import dbos_params
from django_dbos.tasks_registry import aexecute_task_body, execute_task_body
from django_dbos.workflow_names import workflow_name_for_task

from .conftest import wait_for_result
from .helpers import make_status

# Events recorded per workflow id: other workflows (cron firings) run in the
# same worker during the session and must not pollute a test's sequence.
events: defaultdict[str, list[str]] = defaultdict(list)


def record(event: str) -> None:
    events[DBOS.workflow_id or ''].append(event)


@task(backend='dbos')
def recording_add(a: int, b: int) -> int:
    record('body')
    return a + b


@task(backend='dbos')
def failing() -> None:
    record('body')
    raise ValueError('boom')


@dbos_params(durable_operations=True)
@task(backend='dbos')
def durable() -> None:
    record('body')


@task(backend='dbos')
async def async_recording_add(a: int, b: int) -> int:
    record('body')
    return a + b


@task(backend='dbos')
async def async_failing() -> None:
    record('body')
    raise ValueError('boom')


@dbos_params(durable_operations=True)
@task(backend='dbos', takes_context=True)
async def async_durable_context(context: TaskContext, a: int) -> dict[str, object]:
    record('body')
    return {'attempt': context.attempt, 'args': context.task_result.args}


# Durable operations keep the body out of DBOS.run_step, so these run
# directly in the unit tests below, without a launched DBOS.
@dbos_params(durable_operations=True)
@task(backend='dbos', takes_context=True)
def attempt_task(context: TaskContext) -> TaskContext:
    return context


@dbos_params(durable_operations=True)
@task(backend='dbos', takes_context=True)
async def async_attempt_task(context: TaskContext) -> TaskContext:
    return context


def running_status(task, **fields):
    return make_status(
        name=workflow_name_for_task(task),
        status='PENDING',
        input={'args': [], 'kwargs': {}},
        output=None,
        completed_at=None,
        **fields,
    )


class TestTaskContext:
    """``context.attempt`` counts executions, read from the system database."""

    @pytest.fixture
    def client(self, mocker):
        client = MagicMock()
        # A connection left open by an earlier test would be checked, and database access is blocked here.
        mocker.patch('django_dbos.tasks_registry.close_old_connections')
        mocker.patch('django_dbos.tasks_registry.get_client', return_value=client)
        mocker.patch('django_dbos.tasks_registry.DBOS.workflow_id', 'wf-1')
        return client

    def test_attempt_counts_recoveries(self, client):
        client.retrieve_workflow.return_value.get_status.return_value = running_status(
            attempt_task, recovery_attempts=3
        )

        context = execute_task_body(attempt_task, (), {})

        client.retrieve_workflow.assert_called_once_with('wf-1')
        assert context.attempt == 3
        assert context.task_result.worker_ids == ['worker-1'] * 3
        assert context.task_result.status == TaskResultStatus.RUNNING

    def test_async_attempt_counts_recoveries(self, client):
        handle = MagicMock()
        handle.get_status = AsyncMock(return_value=running_status(async_attempt_task, recovery_attempts=2))
        client.retrieve_workflow_async = AsyncMock(return_value=handle)

        context = asyncio.run(aexecute_task_body(async_attempt_task, (), {}))

        client.retrieve_workflow_async.assert_awaited_once_with('wf-1')
        assert context.attempt == 2

    def test_unreadable_status_falls_back_to_a_first_attempt(self, client):
        client.retrieve_workflow.side_effect = RuntimeError('database down')

        with capture_logs() as logs:
            context = execute_task_body(attempt_task, (), {})

        assert context.attempt == 1
        assert any(log['event'].startswith('could not read the task status') for log in logs)

    def test_async_unreadable_status_falls_back_to_a_first_attempt(self, client):
        client.retrieve_workflow_async = AsyncMock(side_effect=RuntimeError('database down'))

        context = asyncio.run(aexecute_task_body(async_attempt_task, (), {}))

        assert context.attempt == 1


@pytest.mark.integration
class TestExecution:
    """Task bodies executed by the session's DBOS worker."""

    @pytest.fixture(autouse=True)
    def record_connection_recycling(self, dbos_environment, mocker):
        mocker.patch('django_dbos.tasks_registry.close_old_connections', side_effect=lambda: record('close'))

    @pytest.mark.parametrize(
        ('task', 'args', 'expected'),
        [
            pytest.param(recording_add, (1, 2), 3, id='sync-step'),
            pytest.param(durable, (), None, id='sync-durable'),
            pytest.param(async_recording_add, (2, 3), 5, id='async-step'),
        ],
    )
    def test_recycles_connections_around_the_body(self, task, args, expected):
        result = wait_for_result(task.enqueue(*args), TaskResultStatus.SUCCESSFUL)

        assert result.return_value == expected
        assert events[result.id] == ['close', 'body', 'close']

    @pytest.mark.parametrize('task', [failing, async_failing], ids=['sync', 'async'])
    def test_recycles_connections_even_when_the_task_fails(self, task):
        result = wait_for_result(task.enqueue(), TaskResultStatus.FAILED)

        assert events[result.id] == ['close', 'body', 'close']

    def test_async_durable_task_receives_its_context(self):
        result = wait_for_result(async_durable_context.enqueue(7), TaskResultStatus.SUCCESSFUL)

        assert result.return_value == {'attempt': 1, 'args': [7]}
        assert events[result.id] == ['close', 'body', 'close']
