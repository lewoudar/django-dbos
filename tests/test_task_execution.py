"""Tests for the task body execution helpers (connection hygiene, context, step vs workflow level)."""

from __future__ import annotations

import asyncio

import pytest
from django.tasks import task

from django_dbos import dbos_params
from django_dbos.tasks_registry import aexecute_task_body, execute_task_body

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


@pytest.fixture
def calls(mocker):
    """Record close_old_connections() calls relative to the task body, with steps run inline."""
    events.clear()
    mocker.patch('django_dbos.tasks_registry.close_old_connections', side_effect=lambda: events.append('close'))
    mocker.patch('django_dbos.tasks_registry.DBOS.run_step', side_effect=lambda options, func, *a, **kw: func(*a, **kw))

    async def run_step_async(options, func, *a, **kw):
        return await func(*a, **kw)

    mocker.patch('django_dbos.tasks_registry.DBOS.run_step_async', side_effect=run_step_async)
    return events


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
