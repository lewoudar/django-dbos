"""Tasks used by the test suite (module-level, as required by django.tasks)."""

import asyncio

from django.tasks import task

from django_dbos import dbos_params

results: dict[str, list[object]] = {"events": []}
flaky_calls: list[int] = []


@task(backend="dbos")
def add(a: int, b: int) -> int:
    return a + b


@task(backend="dbos")
def failing_task() -> None:
    raise ValueError("boom")


@task(backend="dbos", queue_name="emails")
def send_email(address: str) -> str:
    return f"sent to {address}"


@task(backend="dbos")
async def async_add(a: int, b: int) -> int:
    await asyncio.sleep(0)
    return a + b


@task(backend="dbos")
@dbos_params(step_retries_allowed=True, step_max_attempts=2, step_interval_seconds=0.1)
def flaky_with_retries() -> int:
    flaky_calls.append(1)
    if len(flaky_calls) < 2:
        raise RuntimeError("first attempt fails")
    return len(flaky_calls)
