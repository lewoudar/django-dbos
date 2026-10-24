"""Tasks used by the test suite (module-level, as required by django.tasks)."""

import asyncio
import time

from django.tasks import task

from django_dbos import cron, dbos_params, get_dbos_context

results: dict[str, list[object]] = {'events': []}
flaky_calls: list[int] = []
cron_runs: list[str] = []


@task(backend='dbos')
def add(a: int, b: int) -> int:
    return a + b


@task(backend='dbos')
def failing_task() -> None:
    raise ValueError('boom')


@task(backend='dbos', queue_name='emails')
def send_email(address: str) -> str:
    return f'sent to {address}'


@task(backend='dbos', queue_name='per_user')
@dbos_params(partition_key='user_id')
def greet_user(user_id: int, greeting: str = 'hello') -> str:
    return f'{greeting} {user_id}'


@task(backend='dbos')
async def async_add(a: int, b: int) -> int:
    await asyncio.sleep(0)
    return a + b


@task(backend='dbos')
@dbos_params(deduplication_id=lambda user_id: f'sync-user-{user_id}')
def sync_user(user_id: int) -> int:
    return user_id


@task(backend='dbos', takes_context=True)
def context_task(context: object) -> bool:
    return context is not None


@task(backend='dbos')
@dbos_params(step_retries_allowed=True, step_max_attempts=2, step_interval_seconds=0.1)
def flaky_with_retries() -> int:
    flaky_calls.append(1)
    if len(flaky_calls) < 2:
        raise RuntimeError('first attempt fails')
    return len(flaky_calls)


def record_step(event: str) -> str:
    results['events'] = results['events'] + [event]
    return event


def flaky_step() -> int:
    flaky_calls.append(1)
    if len(flaky_calls) < 2:
        raise RuntimeError('first attempt fails')
    return len(flaky_calls)


@task(backend='dbos')
@dbos_params(durable_operations=True)
def durable_step_with_retries() -> int:
    step_options = {'retries_allowed': True, 'max_attempts': 2, 'interval_seconds': 0.1}
    return get_dbos_context().run_step(flaky_step, step_options=step_options)


@task(backend='dbos')
@dbos_params(durable_operations=True)
def durable_onboarding() -> list[str]:
    ctx = get_dbos_context()
    ctx.run_step(record_step, 'step1', name='first_step')
    ctx.set_event('onboarded', True)
    ctx.run_step(record_step, 'step2', name='second_step')
    return results['events']  # type: ignore[return-value]


@task(backend='dbos')
@dbos_params(durable_operations=True)
def streaming_task(count: int) -> dict[str, object]:
    ctx = get_dbos_context()
    for index in range(count):
        ctx.write_stream('progress', index)
    ctx.close_stream('progress')
    ctx.set_event('first', 1)
    ctx.set_event('second', 2)
    return ctx.get_all_events(ctx.workflow_id)


@task(backend='dbos')
@dbos_params(durable_operations=True)
async def async_streaming_task(count: int) -> dict[str, object]:
    ctx = get_dbos_context()
    for index in range(count):
        await ctx.write_stream_async('progress', index)
    await ctx.close_stream_async('progress')
    await ctx.set_event_async('first', 1)
    await ctx.set_event_async('second', 2)
    return await ctx.get_all_events_async(ctx.workflow_id)


@task(backend='dbos')
def context_without_durable_operations() -> None:
    get_dbos_context()  # runs as a step: durable_operations is missing


@task(backend='dbos', takes_context=True)
@dbos_params(durable_operations=True)
def durable_context_task(context: object) -> dict[str, object]:
    ctx = get_dbos_context()
    ctx.run_step(record_step, 'durable-context-step')
    ctx.set_event('attempt', context.attempt)
    return {'attempt': context.attempt, 'events': list(results['events'])}


@cron('* * * * * *')  # every second, for fast tests
@task(backend='dbos')
def scheduled_cleanup() -> str:
    cron_runs.append('ran')
    return 'cleaned'


@cron('*/5 * * * *')
@task(backend='dbos')
def cron_task() -> None: ...


@dbos_params(workflow_timeout=1)
@task(backend='dbos')
def slow_task() -> None:
    time.sleep(2)


@cron('*/5 * * * * *')  # every 5 seconds: each firing keeps sleeping 2 s once cancelled
@dbos_params(workflow_timeout=1)
@task(backend='dbos')
def slow_cron_task() -> None:
    time.sleep(2)


# Scheduled-workflow body for the async cron path; deliberately not decorated
# with @cron so it does not fire during the integration suite.
@task(backend='dbos')
async def async_cron_body() -> str:
    await asyncio.sleep(0)
    return 'async cron ran'


export_calls: list[str] = []
export_failures: set[int] = set()


def list_export_ids() -> list[int]:
    export_calls.append('list')
    return [1, 2, 3, 4]


def render_export_line(order_id: int) -> str:
    export_calls.append(f'render-{order_id}')
    if order_id in export_failures:
        raise RuntimeError(f'cannot render order {order_id}')
    return f'order {order_id}'


@task(backend='dbos')
@dbos_params(durable_operations=True)
def export_task() -> int:
    ctx = get_dbos_context()
    order_ids = ctx.run_step(list_export_ids)
    for order_id in order_ids:
        ctx.write_stream('lines', ctx.run_step(render_export_line, order_id))
    ctx.close_stream('lines')
    return len(order_ids)
