"""Tests for the public test helpers (django_dbos.test)."""

import asyncio

from django.test import SimpleTestCase

from django_dbos.test import arun_task, run_task

from .tasks import add, async_add


class RunTaskTest(SimpleTestCase):
    def test_run_task_executes_synchronously(self):
        assert run_task(add, 2, 3) == 5

    def test_arun_task_executes_async_task(self):
        assert asyncio.run(arun_task(async_add, 10, 20)) == 30
