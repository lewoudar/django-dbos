"""Tests for the public test helpers (django_dbos.test)."""

import asyncio

from django_dbos.test import arun_task, run_task

from .tasks import add, async_add


def test_run_task_executes_synchronously():
    assert run_task(add, 2, 3) == 5


def test_arun_task_executes_async_task():
    assert asyncio.run(arun_task(async_add, 10, 20)) == 30
