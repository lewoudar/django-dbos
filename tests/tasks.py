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


@task(backend='dbos')
async def async_add(a: int, b: int) -> int:
    await asyncio.sleep(0)
    return a + b


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


@task(backend='dbos')
@dbos_params(durable_operations=True)
def durable_onboarding() -> list[str]:
    ctx = get_dbos_context()
    ctx.run_step(record_step, 'step1', name='first_step')
    ctx.set_event('onboarded', True)
    ctx.run_step(record_step, 'step2', name='second_step')
    return results['events']  # type: ignore[return-value]


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
