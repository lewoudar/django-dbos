"""Unit tests for the durable task context (mocked DBOS primitives)."""

import asyncio
from unittest.mock import call

import pytest

from django_dbos.context import DbosTaskContext, NoActiveDbosContext, get_dbos_context


@pytest.fixture()
def ctx() -> DbosTaskContext:
    return DbosTaskContext(workflow_id='w-1')


def some_workflow() -> None: ...


def _step_options(run_step_mock):
    """StepOptions is a TypedDict: inspect it as a dict."""
    return run_step_mock.call_args[0][0]


class TestRunStep:
    def test_passes_positional_args_and_returns_result(self, ctx, mocker):
        run_step = mocker.patch('django_dbos.context.DBOS.run_step', return_value=42)
        assert ctx.run_step(lambda x: x, 7) == 42
        assert run_step.call_args[0][2:] == (7,)

    def test_named_step_sets_option(self, ctx, mocker):
        run_step = mocker.patch('django_dbos.context.DBOS.run_step')
        ctx.run_step(lambda: None, name='step-name')
        assert _step_options(run_step).get('name') == 'step-name'

    def test_unnamed_step_has_no_name_option(self, ctx, mocker):
        run_step = mocker.patch('django_dbos.context.DBOS.run_step')
        ctx.run_step(lambda: None)
        assert _step_options(run_step).get('name') is None

    def test_run_step_async(self, ctx, mocker):
        run_step_async = mocker.patch('django_dbos.context.DBOS.run_step_async', return_value=3)
        assert asyncio.run(ctx.run_step_async(lambda: None, name='a')) == 3
        assert _step_options(run_step_async).get('name') == 'a'


class TestDurablePrimitives:
    def test_sleep(self, ctx, mocker):
        sleep = mocker.patch('django_dbos.context.DBOS.sleep')
        ctx.sleep(86400)
        assert sleep.call_args == call(86400)

    def test_sleep_async(self, ctx, mocker):
        sleep_async = mocker.patch('django_dbos.context.DBOS.sleep_async', return_value=None)
        asyncio.run(ctx.sleep_async(5))
        assert sleep_async.call_args == call(5)

    def test_set_event(self, ctx, mocker):
        set_event = mocker.patch('django_dbos.context.DBOS.set_event')
        ctx.set_event('ready', True)
        assert set_event.call_args == call('ready', True)

    def test_get_event(self, ctx, mocker):
        get_event = mocker.patch('django_dbos.context.DBOS.get_event', return_value='value')
        assert ctx.get_event('other-wf', 'key', timeout_seconds=10) == 'value'
        assert get_event.call_args == call('other-wf', 'key', 10)

    def test_send(self, ctx, mocker):
        send = mocker.patch('django_dbos.context.DBOS.send')
        ctx.send('dest-wf', 'hello', topic='greetings')
        assert send.call_args == call('dest-wf', 'hello', 'greetings')

    def test_recv(self, ctx, mocker):
        recv = mocker.patch('django_dbos.context.DBOS.recv', return_value='msg')
        assert ctx.recv(topic='greetings', timeout_seconds=5) == 'msg'
        assert recv.call_args == call('greetings', 5)

    def test_start_child_workflow(self, ctx, mocker):
        start = mocker.patch('django_dbos.context.DBOS.start_workflow', return_value='handle')
        assert ctx.start_child_workflow(some_workflow, 1, kw=2) == 'handle'
        assert start.call_args == call(some_workflow, 1, kw=2)


class TestGetDbosContext:
    def test_raises_outside_workflow(self, mocker):
        mocker.patch('django_dbos.context.DBOS.workflow_id', None)
        with pytest.raises(NoActiveDbosContext):
            get_dbos_context()

    def test_raises_inside_a_step(self, mocker):
        # A task without durable_operations runs its whole body as a step.
        mocker.patch('django_dbos.context.DBOS.workflow_id', 'wf-123')
        mocker.patch('django_dbos.context.DBOS.step_id', 1)
        with pytest.raises(NoActiveDbosContext, match='durable_operations=True'):
            get_dbos_context()

    def test_returns_context_with_workflow_id(self, mocker):
        mocker.patch('django_dbos.context.DBOS.workflow_id', 'wf-123')
        mocker.patch('django_dbos.context.DBOS.step_id', None)
        assert get_dbos_context().workflow_id == 'wf-123'
