"""Tests for the task body execution helpers (connection hygiene, context, step vs workflow level)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from django.tasks import TaskContext, TaskResultStatus, task
from structlog.testing import capture_logs

from django_dbos import dbos_params
from django_dbos.tasks_registry import aexecute_task_body, execute_task_body, workflow_name_for_task

from .tasks import context_task

events: list[str] = []


@task(backend='dbos')
def recording_add(a: int, b: int) -> int:
    events.append('body')
    return a + b


@task(backend='dbos')
def failing() -> None:
    events.append('body')
    raise ValueError('boom')


@dbos_params(durable_operations=True)
@task(backend='dbos')
def durable() -> None:
    events.append('body')


@task(backend='dbos')
async def async_recording_add(a: int, b: int) -> int:
    events.append('body')
    return a + b


@dbos_params(durable_operations=True)
@task(backend='dbos', takes_context=True)
async def async_durable_context(context: TaskContext, a: int) -> list[object]:
    events.append('body')
    return context.task_result.args


@task(backend='dbos', takes_context=True)
def attempt_task(context: TaskContext) -> TaskContext:
    return context


@task(backend='dbos', takes_context=True)
async def async_attempt_task(context: TaskContext) -> TaskContext:
    return context


@pytest.fixture
def calls(mocker):
    """Record close_old_connections() calls relative to the task body, with steps run inline."""
    events.clear()
    mocker.patch('django_dbos.tasks_registry.close_old_connections', side_effect=lambda: events.append('close'))
    mocker.patch('django_dbos.tasks_registry.DBOS.run_step', side_effect=lambda options, func, *a, **kw: func(*a, **kw))

    async def run_step_async(options, func, *a, **kw):
        return await func(*a, **kw)

    mocker.patch('django_dbos.tasks_registry.DBOS.run_step_async', side_effect=run_step_async)
    # No system database here: building a TaskContext falls back to a single attempt.
    mocker.patch('django_dbos.client.get_client', side_effect=RuntimeError('no system database'))
    return events


def workflow_status(task, **fields):
    values = {
        'status': 'PENDING',
        'name': workflow_name_for_task(task),
        'error': None,
        'input': {'args': [], 'kwargs': {}},
        'output': None,
        'created_at': 1_700_000_000_000,
        'updated_at': 1_700_000_000_500,
        'dequeued_at': 1_700_000_000_100,
        'completed_at': None,
        'workflow_timeout_ms': None,
        'workflow_deadline_epoch_ms': None,
        'recovery_attempts': 1,
        'executor_id': 'worker-1',
    }
    return SimpleNamespace(**(values | fields))


class TestTaskContext:
    """``context.attempt`` counts executions, read from the system database."""

    @pytest.fixture
    def client(self, calls, mocker):
        client = MagicMock()
        mocker.patch('django_dbos.client.get_client', return_value=client)
        mocker.patch('django_dbos.tasks_registry.DBOS.workflow_id', 'wf-1')
        return client

    def test_attempt_counts_recoveries(self, client):
        client.retrieve_workflow.return_value.get_status.return_value = workflow_status(
            attempt_task, recovery_attempts=3
        )

        context = execute_task_body(attempt_task, (), {})

        client.retrieve_workflow.assert_called_once_with('wf-1')
        assert context.attempt == 3
        assert context.task_result.worker_ids == ['worker-1'] * 3
        assert context.task_result.status == TaskResultStatus.RUNNING

    def test_async_attempt_counts_recoveries(self, client):
        handle = MagicMock()
        handle.get_status = AsyncMock(return_value=workflow_status(async_attempt_task, recovery_attempts=2))
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


class TestExecuteTaskBody:
    def test_recycles_connections_around_the_step(self, calls):
        assert execute_task_body(recording_add, (1, 2), {}) == 3
        assert calls == ['close', 'body', 'close']

    def test_recycles_connections_even_when_the_task_fails(self, calls):
        with pytest.raises(ValueError, match='boom'):
            execute_task_body(failing, (), {})
        assert calls == ['close', 'body', 'close']

    def test_durable_operations_run_at_workflow_level(self, calls, mocker):
        run_step = mocker.patch('django_dbos.tasks_registry.DBOS.run_step')
        execute_task_body(durable, (), {})
        run_step.assert_not_called()
        assert calls == ['close', 'body', 'close']

    def test_context_is_prepended(self, calls):
        assert execute_task_body(context_task, (), {}) is True

    def test_async_task_recycles_connections(self, calls):
        assert asyncio.run(aexecute_task_body(async_recording_add, (2, 3), {})) == 5
        assert calls == ['close', 'body', 'close']

    def test_async_durable_task_with_context_runs_at_workflow_level(self, calls, mocker):
        run_step_async = mocker.patch('django_dbos.tasks_registry.DBOS.run_step_async')
        assert asyncio.run(aexecute_task_body(async_durable_context, (7,), {})) == [7]
        run_step_async.assert_not_called()
        assert calls == ['close', 'body', 'close']
