"""Unit tests for the durable task context (mocked DBOS primitives)."""

import asyncio
from unittest.mock import call

import pytest

from django_dbos.context import DbosTaskContext, NoActiveDbosContext, as_completed, get_dbos_context


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

    def test_step_options_are_passed_to_dbos_not_to_func(self, ctx, mocker):
        run_step = mocker.patch('django_dbos.context.DBOS.run_step')
        step_options = {'retries_allowed': True, 'max_attempts': 5}
        ctx.run_step(lambda x: x, 7, step_options=step_options, extra=1)
        assert _step_options(run_step) == step_options
        assert run_step.call_args[0][2:] == (7,)
        assert run_step.call_args[1] == {'extra': 1}

    def test_name_overrides_the_step_options_name(self, ctx, mocker):
        run_step = mocker.patch('django_dbos.context.DBOS.run_step')
        step_options = {'name': 'from-options', 'retries_allowed': True}
        ctx.run_step(lambda: None, name='from-arg', step_options=step_options)
        assert _step_options(run_step) == {'name': 'from-arg', 'retries_allowed': True}
        assert step_options['name'] == 'from-options'  # the caller's dict is not mutated

    def test_run_step_async(self, ctx, mocker):
        run_step_async = mocker.patch('django_dbos.context.DBOS.run_step_async', return_value=3)
        assert asyncio.run(ctx.run_step_async(lambda: None, name='a')) == 3
        assert _step_options(run_step_async).get('name') == 'a'

    def test_run_step_async_with_step_options(self, ctx, mocker):
        run_step_async = mocker.patch('django_dbos.context.DBOS.run_step_async', return_value=3)
        asyncio.run(ctx.run_step_async(lambda: None, step_options={'retries_allowed': True}))
        assert _step_options(run_step_async) == {'retries_allowed': True}


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

    def test_set_event_async(self, ctx, mocker):
        set_event_async = mocker.patch('django_dbos.context.DBOS.set_event_async', return_value=None)
        asyncio.run(ctx.set_event_async('ready', True))
        assert set_event_async.call_args == call('ready', True)

    def test_get_event_async(self, ctx, mocker):
        get_event_async = mocker.patch('django_dbos.context.DBOS.get_event_async', return_value='value')
        assert asyncio.run(ctx.get_event_async('other-wf', 'key', timeout_seconds=10)) == 'value'
        assert get_event_async.call_args == call('other-wf', 'key', 10)

    def test_get_all_events(self, ctx, mocker):
        get_all_events = mocker.patch('django_dbos.context.DBOS.get_all_events', return_value={'a': 1})
        assert ctx.get_all_events('other-wf') == {'a': 1}
        assert get_all_events.call_args == call('other-wf')

    def test_get_all_events_async(self, ctx, mocker):
        get_all_events_async = mocker.patch('django_dbos.context.DBOS.get_all_events_async', return_value={'a': 1})
        assert asyncio.run(ctx.get_all_events_async('other-wf')) == {'a': 1}
        assert get_all_events_async.call_args == call('other-wf')

    def test_send_async(self, ctx, mocker):
        send_async = mocker.patch('django_dbos.context.DBOS.send_async', return_value=None)
        asyncio.run(ctx.send_async('dest-wf', 'hello', topic='greetings'))
        assert send_async.call_args == call('dest-wf', 'hello', 'greetings')

    def test_recv_async(self, ctx, mocker):
        recv_async = mocker.patch('django_dbos.context.DBOS.recv_async', return_value='msg')
        assert asyncio.run(ctx.recv_async(topic='greetings', timeout_seconds=5)) == 'msg'
        assert recv_async.call_args == call('greetings', 5)

    def test_start_child_workflow(self, ctx, mocker):
        start = mocker.patch('django_dbos.context.DBOS.start_workflow', return_value='handle')
        assert ctx.start_child_workflow(some_workflow, 1, kw=2) == 'handle'
        assert start.call_args == call(some_workflow, 1, kw=2)

    def test_wait_first(self, ctx, mocker):
        wait_first = mocker.patch('django_dbos.context.DBOS.wait_first', return_value='h2')
        assert ctx.wait_first(['h1', 'h2']) == 'h2'
        assert wait_first.call_args == call(['h1', 'h2'])  # DBOS' own default interval

    def test_wait_first_with_polling_interval(self, ctx, mocker):
        wait_first = mocker.patch('django_dbos.context.DBOS.wait_first', return_value='h1')
        ctx.wait_first(['h1'], polling_interval_sec=0.1)
        assert wait_first.call_args == call(['h1'], polling_interval_sec=0.1)


class TestStreams:
    def test_write_stream(self, ctx, mocker):
        write_stream = mocker.patch('django_dbos.context.DBOS.write_stream')
        ctx.write_stream('progress', 1)
        assert write_stream.call_args == call('progress', 1)

    def test_write_stream_async(self, ctx, mocker):
        write_stream_async = mocker.patch('django_dbos.context.DBOS.write_stream_async', return_value=None)
        asyncio.run(ctx.write_stream_async('progress', 1))
        assert write_stream_async.call_args == call('progress', 1)

    def test_close_stream(self, ctx, mocker):
        close_stream = mocker.patch('django_dbos.context.DBOS.close_stream')
        ctx.close_stream('progress')
        assert close_stream.call_args == call('progress')

    def test_close_stream_async(self, ctx, mocker):
        close_stream_async = mocker.patch('django_dbos.context.DBOS.close_stream_async', return_value=None)
        asyncio.run(ctx.close_stream_async('progress'))
        assert close_stream_async.call_args == call('progress')

    def test_read_stream(self, ctx, mocker):
        read_stream = mocker.patch('django_dbos.context.DBOS.read_stream', return_value=iter([1, 2]))
        assert list(ctx.read_stream('other-wf', 'progress', offset=1, timeout_seconds=5)) == [1, 2]
        assert read_stream.call_args == call('other-wf', 'progress', offset=1, timeout_seconds=5)

    def test_read_stream_async(self, ctx, mocker):
        async def values(*args, **kwargs):
            for value in (1, 2):
                yield value

        read_stream_async = mocker.patch('django_dbos.context.DBOS.read_stream_async', side_effect=values)

        async def read():
            return [value async for value in ctx.read_stream_async('other-wf', 'progress')]

        assert asyncio.run(read()) == [1, 2]
        assert read_stream_async.call_args == call('other-wf', 'progress', offset=0, timeout_seconds=None)

    def test_read_stream_offset(self, ctx, mocker):
        read_stream_offset = mocker.patch('django_dbos.context.DBOS.read_stream_offset', return_value=2)
        assert ctx.read_stream_offset('other-wf', 'progress', 1, timeout_seconds=5) == 2
        assert read_stream_offset.call_args == call('other-wf', 'progress', 1, timeout_seconds=5)

    def test_read_stream_offset_async(self, ctx, mocker):
        read_stream_offset_async = mocker.patch('django_dbos.context.DBOS.read_stream_offset_async', return_value=2)
        assert asyncio.run(ctx.read_stream_offset_async('other-wf', 'progress', 1)) == 2
        assert read_stream_offset_async.call_args == call('other-wf', 'progress', 1, timeout_seconds=None)


class TestGetDbosContext:
    def test_raises_outside_workflow(self, mocker):
        mocker.patch('django_dbos.context.DBOS.workflow_id', None)
        with pytest.raises(NoActiveDbosContext, match='must be called from within a running task'):
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


class TestAsCompleted:
    @pytest.fixture(autouse=True)
    def in_workflow(self, mocker):
        mocker.patch('django_dbos.context.DBOS.workflow_id', 'wf-123')
        mocker.patch('django_dbos.context.DBOS.step_id', None)

    def test_yields_handles_in_completion_order(self, mocker):
        finished = iter(['h2', 'h3', 'h1'])
        waited_on = []

        def wait_first(handles, polling_interval_sec):
            waited_on.append((list(handles), polling_interval_sec))
            return next(finished)

        mocker.patch('django_dbos.context.DBOS.wait_first', side_effect=wait_first)
        assert list(as_completed(iter(['h1', 'h2', 'h3']), polling_interval_sec=0.1)) == ['h2', 'h3', 'h1']
        # Each call waits on the handles still pending.
        assert waited_on == [(['h1', 'h2', 'h3'], 0.1), (['h1', 'h3'], 0.1), (['h1'], 0.1)]

    def test_no_handles(self, mocker):
        wait_first = mocker.patch('django_dbos.context.DBOS.wait_first')
        assert list(as_completed([])) == []
        wait_first.assert_not_called()

    def test_requires_a_durable_task(self, mocker):
        mocker.patch('django_dbos.context.DBOS.workflow_id', None)
        with pytest.raises(NoActiveDbosContext, match='must be called from within a running task'):
            next(as_completed(['h1']))
