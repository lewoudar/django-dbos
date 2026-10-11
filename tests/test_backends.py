"""Tests for the DBOS task backend: helpers (unit) and behavior (integration)."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from django.tasks import TaskResultStatus
from django.tasks.exceptions import TaskResultDoesNotExist
from django.utils import timezone

from django_dbos import dbos_params
from django_dbos.backends import _delay_seconds, _django_to_dbos_priority
from django_dbos.exceptions import QueueNotDeclared
from django_dbos.workflow_names import workflow_name_for_task

from .conftest import wait_for_result
from .tasks import add, async_add, context_task, failing_task

pytestmark = pytest.mark.integration


class TestHelpers:
    @pytest.mark.parametrize(
        ('run_after', 'expected'),
        [
            pytest.param(None, None, id='none'),
            pytest.param(timedelta(seconds=90), 90.0, id='timedelta'),
            pytest.param(timedelta(seconds=-5), 0.0, id='negative-timedelta-clamped'),
        ],
    )
    def test_delay_seconds(self, run_after, expected):
        assert _delay_seconds(run_after) == expected

    def test_delay_seconds_from_datetime(self):
        run_after = datetime.now(tz=UTC) + timedelta(minutes=1)
        assert 0 <= _delay_seconds(run_after) <= 60

    @pytest.mark.parametrize(
        ('django_priority', 'expected'),
        [(-100, 201), (0, 101), (100, 1)],
    )
    def test_priority_mapping_preserves_order(self, django_priority, expected):
        assert _django_to_dbos_priority(django_priority) == expected


class TestEnqueueOptions:
    def test_options_carry_every_dbos_param(self):
        task = dbos_params(deduplication_id='dedup', workflow_timeout=90)(add)
        task = task.using(priority=50, run_after=timezone.now() + timedelta(seconds=30))
        options = add.get_backend()._enqueue_options(task)

        assert options['workflow_name'] == workflow_name_for_task(add)
        assert options['queue_name'] == 'default'
        assert options['priority'] == 51
        assert 0 < options['delay_seconds'] <= 30
        assert options['deduplication_id'] == 'dedup'
        assert options['workflow_timeout'] == 90

    def test_options_omit_unset_params(self):
        options = add.get_backend()._enqueue_options(add)
        assert set(options) == {'workflow_name', 'queue_name', 'priority'}


class TestBackendBehavior:
    def test_enqueue_goes_through_the_client_not_dbos(self, dbos_environment, mocker):
        # A web process must never initialize or launch DBOS to enqueue.
        ensure_initialized = mocker.patch('django_dbos.dbos_setup.ensure_initialized')
        ensure_launched = mocker.patch('django_dbos.dbos_setup.ensure_launched')
        enqueue_workflow = mocker.patch('dbos.DBOS.enqueue_workflow')

        result = wait_for_result(add.enqueue(1, 1), TaskResultStatus.SUCCESSFUL)

        assert result.return_value == 2
        ensure_initialized.assert_not_called()
        ensure_launched.assert_not_called()
        enqueue_workflow.assert_not_called()

    def test_get_result_unknown_id_raises(self, dbos_environment):
        with pytest.raises(TaskResultDoesNotExist):
            add.get_result('00000000-0000-0000-0000-000000000000')

    def test_get_result_database_errors_are_not_masked(self, dbos_environment, mocker):
        mocker.patch('django_dbos.client.get_client', side_effect=RuntimeError('db unreachable'))
        mocker.patch('django_dbos.backends.get_client', side_effect=RuntimeError('db unreachable'))
        with pytest.raises(RuntimeError, match='db unreachable'):
            add.get_result('00000000-0000-0000-0000-000000000000')

    def test_undeclared_queue_is_rejected(self, dbos_environment, settings):
        # django.tasks validates the queue against TASKS at construction, but
        # the backend requires it in DJANGO_DBOS['QUEUES'] (where workers are
        # configured): a task on a queue no worker declares must fail loudly.
        settings.TASKS = {
            **settings.TASKS,
            'dbos': {**settings.TASKS['dbos'], 'QUEUES': [*settings.TASKS['dbos']['QUEUES'], 'ghost_queue']},
        }
        rogue = replace(add, queue_name='ghost_queue')
        with pytest.raises(QueueNotDeclared):
            rogue.enqueue()

    def test_failed_task_includes_traceback(self, dbos_environment):
        result = wait_for_result(failing_task.enqueue(), TaskResultStatus.FAILED)
        assert result.errors
        error = result.errors[0]
        assert error.exception_class is ValueError
        assert 'boom' in error.traceback

    def test_takes_context_task_receives_context(self, dbos_environment):
        result = wait_for_result(context_task.enqueue(), TaskResultStatus.SUCCESSFUL)
        assert result.return_value is True

    @pytest.mark.skip(
        reason=(
            'DBOS SDK limitation: after an async enqueue on a (now closed) event loop, '
            'subsequent synchronous DBOS calls in the same process fail with '
            "'cannot schedule new futures after shutdown'. Real usage keeps the loop "
            'alive (async views), but the test harness opens and closes a loop per call.'
        )
    )
    def test_async_enqueue_roundtrip(self, dbos_environment):
        result = asyncio.run(async_add.aenqueue(5, 6))
        result = wait_for_result(result, TaskResultStatus.SUCCESSFUL)
        assert result.return_value == 11
