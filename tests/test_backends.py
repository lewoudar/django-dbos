"""Tests for the DBOS task backend: helpers (unit) and behavior (integration)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from django.tasks import TaskResultStatus
from django.tasks.exceptions import InvalidTask, TaskResultDoesNotExist
from django.utils import timezone

from django_dbos import dbos_params
from django_dbos.backends import _dbos_to_django_priority, _delay_seconds, _django_to_dbos_priority
from django_dbos.client import get_client
from django_dbos.exceptions import ImproperlyConfiguredDbos
from django_dbos.workflow_names import workflow_name_for_task

from .conftest import wait_for_result
from .tasks import add, async_add, context_task, failing_task, greet_user

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
        assert _dbos_to_django_priority(expected) == django_priority

    @pytest.mark.parametrize('dbos_priority', [None, 0, 202])
    def test_priority_outside_the_django_scale_maps_to_none(self, dbos_priority):
        assert _dbos_to_django_priority(dbos_priority) is None


@pytest.mark.usefixtures('keep_known_tasks')
class TestEnqueueOptions:
    def test_options_carry_every_dbos_param(self):
        task = dbos_params(deduplication_id='dedup', workflow_timeout=90)(add)
        task = task.using(priority=50, run_after=timezone.now() + timedelta(seconds=30))
        options = add.get_backend()._enqueue_options(task, (1, 2), {})

        assert options['workflow_name'] == workflow_name_for_task(add)
        assert options['queue_name'] == 'default'
        assert options['priority'] == 51
        assert 0 < options['delay_seconds'] <= 30
        assert options['deduplication_id'] == 'dedup'
        assert options['workflow_timeout'] == 90

    def test_a_callable_deduplication_id_is_resolved_per_enqueue(self):
        task = dbos_params(deduplication_id=lambda a, b: f'add-{a}-{b}')(add)
        backend = add.get_backend()

        assert backend._enqueue_options(task, (1, 2), {})['deduplication_id'] == 'add-1-2'
        assert backend._enqueue_options(task, (3,), {'b': 4})['deduplication_id'] == 'add-3-4'

    def test_a_callable_deduplication_id_returning_none_is_omitted(self):
        task = dbos_params(deduplication_id=lambda a, b: None)(add)
        assert 'deduplication_id' not in add.get_backend()._enqueue_options(task, (1, 2), {})

    def test_options_omit_unset_params(self):
        options = add.get_backend()._enqueue_options(add, (1, 2), {})
        assert set(options) == {'workflow_name', 'queue_name', 'priority'}


@pytest.mark.usefixtures('keep_known_tasks')
class TestPartitionedQueues:
    @pytest.mark.parametrize(
        ('args', 'kwargs'),
        [pytest.param((42,), {}, id='positional'), pytest.param((), {'user_id': 42}, id='keyword')],
    )
    def test_the_key_is_read_from_the_arguments(self, args, kwargs):
        options = greet_user.get_backend()._enqueue_options(greet_user, args, kwargs)
        assert options['queue_partition_key'] == '42'

    def test_the_key_is_ignored_on_a_queue_without_partitions(self):
        task = greet_user.using(queue_name='default')
        options = task.get_backend()._enqueue_options(task, (42,), {})
        assert 'queue_partition_key' not in options

    def test_a_task_without_partition_key_is_refused(self):
        task = add.using(queue_name='per_user')
        with pytest.raises(ImproperlyConfiguredDbos, match=r"^Queue 'per_user' is partitioned: task 'add' needs"):
            task.get_backend()._enqueue_options(task, (1, 2), {})

    def test_deduplication_is_refused(self):
        task = dbos_params(partition_key='user_id', deduplication_id='dedup')(greet_user)
        with pytest.raises(ImproperlyConfiguredDbos, match='has a deduplication_id, which DBOS does not support'):
            task.get_backend()._enqueue_options(task, (42,), {})

    def test_an_unknown_argument_name_is_an_invalid_task(self):
        with pytest.raises(InvalidTask, match=r"^partition_key 'nope' is not an argument of task 'greet_user'\.$"):
            dbos_params(partition_key='nope')(greet_user)

    def test_the_context_parameter_is_not_an_argument(self):
        with pytest.raises(InvalidTask, match="partition_key 'context' is not an argument"):
            dbos_params(partition_key='context')(context_task)

    def test_is_partitioned(self):
        backend = greet_user.get_backend()
        assert backend.is_partitioned('per_user')
        assert not backend.is_partitioned('default')

    def test_enqueue_records_the_partition_key(self, dbos_environment):
        result = wait_for_result(greet_user.enqueue(7, greeting='hi'), TaskResultStatus.SUCCESSFUL)
        assert result.return_value == 'hi 7'
        assert get_client().retrieve_workflow(result.id).get_status().queue_partition_key == '7'


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
