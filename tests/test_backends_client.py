"""Backend tests against a mocked ``DBOSClient`` (no system database).

They cover the async paths, which the integration suite cannot exercise
(see the skipped ``test_async_enqueue_roundtrip``), and the error paths the
SDK only reaches through race conditions.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from dbos import error as dbos_error
from django.tasks import TaskResultStatus
from django.tasks.exceptions import TaskResultDoesNotExist

from django_dbos.backends import _build_task_result
from django_dbos.exceptions import MaxRecoveryAttemptsExceeded, TaskCancelled, TaskTimedOut
from django_dbos.tasks_registry import workflow_name_for_task

from .tasks import add, async_add

WORKFLOW_ID = 'wf-1'
CREATED_AT_MS = 1_700_000_000_000


def make_status(**overrides):
    values = {
        'status': 'SUCCESS',
        'name': workflow_name_for_task(add),
        'error': None,
        'input': {'args': [1, 2], 'kwargs': {}},
        'output': 3,
        'created_at': CREATED_AT_MS,
        'updated_at': CREATED_AT_MS + 1_000,
        'dequeued_at': CREATED_AT_MS + 200,
        'completed_at': CREATED_AT_MS + 1_000,
        'workflow_timeout_ms': None,
        'workflow_deadline_epoch_ms': None,
        'recovery_attempts': 1,
    }
    return SimpleNamespace(**(values | overrides))


def ms(value):
    """A result timestamp as epoch milliseconds, to compare with DBOS' values."""
    return None if value is None else round(value.timestamp() * 1000)


def not_found():
    return dbos_error.DBOSNonExistentWorkflowError('workflow', WORKFLOW_ID)


@pytest.fixture
def client(mocker):
    client = MagicMock()
    mocker.patch('django_dbos.backends.get_client', return_value=client)
    return client


def sync_handle(status=None, error=None):
    handle = MagicMock(workflow_id=WORKFLOW_ID)
    handle.get_status.side_effect = error
    handle.get_status.return_value = status
    return handle


def async_handle(status=None, error=None):
    handle = MagicMock(workflow_id=WORKFLOW_ID)
    handle.get_status = AsyncMock(return_value=status, side_effect=error)
    return handle


class TestEnqueue:
    def test_workflow_vanishing_right_after_enqueue_raises(self, client):
        client.enqueue.return_value = sync_handle(error=not_found())

        with pytest.raises(TaskResultDoesNotExist):
            add.enqueue(1, 2)

    def test_already_running_result_keeps_its_timestamps(self, client):
        # A worker may pick the workflow up before get_status() runs.
        client.enqueue.return_value = sync_handle(make_status(status='PENDING', output=None, completed_at=None))

        result = add.enqueue(1, 2)

        assert result.status == TaskResultStatus.RUNNING
        assert ms(result.enqueued_at) == CREATED_AT_MS
        assert ms(result.started_at) == ms(result.last_attempted_at) == CREATED_AT_MS + 200
        assert result.finished_at is None


class TestAsyncEnqueue:
    def test_aenqueue_returns_a_fresh_ready_result(self, client):
        status = make_status(
            status='ENQUEUED',
            name=workflow_name_for_task(async_add),
            output=None,
            dequeued_at=None,
            completed_at=None,
            recovery_attempts=0,
        )
        client.enqueue_async = AsyncMock(return_value=async_handle(status))

        result = asyncio.run(async_add.aenqueue(5, 6))

        options = client.enqueue_async.call_args.args[0]
        assert options['workflow_name'] == workflow_name_for_task(async_add)
        assert client.enqueue_async.call_args.args[1:] == (5, 6)
        assert result.id == WORKFLOW_ID
        assert result.status == TaskResultStatus.READY
        assert ms(result.enqueued_at) == CREATED_AT_MS
        assert result.started_at is None
        assert result.last_attempted_at is None

    def test_aenqueue_workflow_vanishing_raises(self, client):
        client.enqueue_async = AsyncMock(return_value=async_handle(error=not_found()))

        with pytest.raises(TaskResultDoesNotExist):
            asyncio.run(async_add.aenqueue(5, 6))


class TestAsyncGetResult:
    def test_aget_result_builds_the_result(self, client):
        client.retrieve_workflow_async = AsyncMock(return_value=async_handle(make_status()))

        result = asyncio.run(add.aget_result(WORKFLOW_ID))

        client.retrieve_workflow_async.assert_awaited_once_with(WORKFLOW_ID)
        assert result.status == TaskResultStatus.SUCCESSFUL
        assert result.return_value == 3
        assert result.args == [1, 2]

    def test_aget_result_unknown_id_raises(self, client):
        client.retrieve_workflow_async = AsyncMock(side_effect=not_found())

        with pytest.raises(TaskResultDoesNotExist):
            asyncio.run(add.aget_result(WORKFLOW_ID))


class TestBuildTaskResult:
    @pytest.mark.parametrize(
        ('dbos_status', 'expected'),
        [
            ('ENQUEUED', TaskResultStatus.READY),
            ('DELAYED', TaskResultStatus.READY),
            ('PENDING', TaskResultStatus.RUNNING),  # DBOS' status of an executing workflow
            ('SUCCESS', TaskResultStatus.SUCCESSFUL),
            ('ERROR', TaskResultStatus.FAILED),
            ('CANCELLED', TaskResultStatus.FAILED),
            ('MAX_RECOVERY_ATTEMPTS_EXCEEDED', TaskResultStatus.FAILED),
        ],
    )
    def test_status_mapping(self, dbos_status, expected):
        assert _build_task_result(WORKFLOW_ID, make_status(status=dbos_status)).status == expected

    @pytest.mark.parametrize(
        ('fields', 'expected_class', 'message'),
        [
            pytest.param({}, TaskCancelled, 'The task was cancelled.', id='explicit-cancel'),
            pytest.param(
                # deadline set but not reached: cancelled from outside before its timeout
                {'workflow_timeout_ms': 60_000, 'workflow_deadline_epoch_ms': CREATED_AT_MS + 60_000},
                TaskCancelled,
                'The task was cancelled.',
                id='cancelled-before-its-deadline',
            ),
            pytest.param(
                {'workflow_timeout_ms': 500, 'workflow_deadline_epoch_ms': CREATED_AT_MS + 500},
                TaskTimedOut,
                'The task exceeded its workflow_timeout of 0.5s.',
                id='workflow-timeout',
            ),
        ],
    )
    def test_cancelled_workflow_reports_why(self, fields, expected_class, message):
        # DBOS stores no error for a cancelled workflow: FAILED must not come without an explanation.
        result = _build_task_result(WORKFLOW_ID, make_status(status='CANCELLED', output=None, **fields))

        assert result.status == TaskResultStatus.FAILED
        [error] = result.errors
        assert error.exception_class is expected_class
        assert error.traceback == repr(expected_class(message))

    def test_exhausted_recovery_reports_why(self):
        # DBOS stores no error when it gives up after repeated crashes either.
        status = make_status(status='MAX_RECOVERY_ATTEMPTS_EXCEEDED', output=None, recovery_attempts=3)

        result = _build_task_result(WORKFLOW_ID, status)

        assert result.status == TaskResultStatus.FAILED
        [error] = result.errors
        assert error.exception_class is MaxRecoveryAttemptsExceeded
        assert '3 execution or recovery attempts' in error.traceback

    def test_unknown_dbos_status_is_not_exposed(self):
        with pytest.raises(TaskResultDoesNotExist):
            _build_task_result(WORKFLOW_ID, make_status(status='SOMETHING_NEW'))

    @pytest.mark.parametrize('name', ['not.a.task.workflow', None])
    def test_workflow_that_is_not_a_task_is_not_exposed(self, name):
        with pytest.raises(TaskResultDoesNotExist):
            _build_task_result(WORKFLOW_ID, make_status(name=name))

    @pytest.mark.parametrize(
        ('fields', 'started', 'finished'),
        [
            pytest.param(
                {'status': 'ENQUEUED', 'dequeued_at': None, 'completed_at': None, 'recovery_attempts': 0},
                None,
                None,
                id='waiting',
            ),
            pytest.param({'status': 'PENDING', 'completed_at': None}, 200, None, id='running'),
            pytest.param({}, 200, 1_000, id='successful'),
            pytest.param(
                # a cron firing runs the task as a child started on the spot, without a queue
                {'dequeued_at': None},
                0,
                1_000,
                id='never-queued',
            ),
            pytest.param(
                {
                    'status': 'CANCELLED',
                    'dequeued_at': None,
                    'recovery_attempts': 0,
                    'completed_at': CREATED_AT_MS + 50,
                },
                None,
                50,
                id='cancelled-before-starting',
            ),
            pytest.param(
                # cancelling clears dequeued_at: the creation time is the best estimate left
                {'status': 'CANCELLED', 'dequeued_at': None},
                0,
                1_000,
                id='cancelled-while-running',
            ),
            pytest.param(
                # DBOS sets no completed_at when it gives up recovering a workflow
                {'status': 'MAX_RECOVERY_ATTEMPTS_EXCEEDED', 'completed_at': None, 'recovery_attempts': 3},
                200,
                1_000,
                id='recovery-exhausted',
            ),
        ],
    )
    def test_timestamps(self, fields, started, finished):
        result = _build_task_result(WORKFLOW_ID, make_status(output=None, **fields))

        assert ms(result.enqueued_at) == CREATED_AT_MS
        assert ms(result.started_at) == (None if started is None else CREATED_AT_MS + started)
        assert result.last_attempted_at == result.started_at
        assert ms(result.finished_at) == (None if finished is None else CREATED_AT_MS + finished)

    def test_missing_timestamps_and_inputs(self):
        result = _build_task_result(
            WORKFLOW_ID, make_status(status='PENDING', created_at=None, dequeued_at=None, input=None, output=None)
        )

        assert result.status == TaskResultStatus.RUNNING
        assert result.enqueued_at is None
        assert result.last_attempted_at is None
        assert result.args == []
        assert result.kwargs == {}

    def test_successful_result_without_output(self):
        result = _build_task_result(WORKFLOW_ID, make_status(output=None))

        assert result.status == TaskResultStatus.SUCCESSFUL
        assert result.return_value is None
