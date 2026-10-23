"""Tests for reading a DBOS workflow status back as a Django ``TaskResult``."""

import pytest
from django.tasks import TaskResultStatus
from django.tasks.exceptions import TaskResultDoesNotExist

from django_dbos.exceptions import MaxRecoveryAttemptsExceeded, TaskCancelled, TaskFailure, TaskTimedOut
from django_dbos.results import build_task_result, is_cancelled, is_recovery_exhausted, is_timed_out

from .helpers import CREATED_AT_MS, WORKFLOW_ID, make_status, ms


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
        assert build_task_result(WORKFLOW_ID, make_status(status=dbos_status)).status == expected

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
        result = build_task_result(WORKFLOW_ID, make_status(status='CANCELLED', output=None, **fields))

        assert result.status == TaskResultStatus.FAILED
        [error] = result.errors
        assert error.exception_class is expected_class
        assert error.traceback == repr(expected_class(message))

    def test_exhausted_recovery_reports_why(self):
        # DBOS stores no error when it gives up after repeated crashes either.
        status = make_status(status='MAX_RECOVERY_ATTEMPTS_EXCEEDED', output=None, recovery_attempts=3)

        result = build_task_result(WORKFLOW_ID, status)

        assert result.status == TaskResultStatus.FAILED
        [error] = result.errors
        assert error.exception_class is MaxRecoveryAttemptsExceeded
        assert '3 execution or recovery attempts' in error.traceback

    @pytest.mark.parametrize(
        ('traceback', 'expected'),
        [('Traceback ...\nValueError: boom\n', 'Traceback ...\nValueError: boom\n'), ('', 'builtins.ValueError: boom')],
        ids=['with-traceback', 'without-traceback'],
    )
    def test_error_stored_without_its_class_reports_the_original_one(self, traceback, expected):
        error = TaskFailure('builtins.ValueError', 'boom', traceback)

        result = build_task_result(WORKFLOW_ID, make_status(status='ERROR', output=None, error=error))

        [task_error] = result.errors
        assert task_error.exception_class_path == 'builtins.ValueError'
        assert task_error.exception_class is ValueError
        assert task_error.traceback == expected

    def test_unknown_dbos_status_is_not_exposed(self):
        with pytest.raises(TaskResultDoesNotExist):
            build_task_result(WORKFLOW_ID, make_status(status='SOMETHING_NEW'))

    @pytest.mark.parametrize('name', ['not.a.task.workflow', None])
    def test_workflow_that_is_not_a_task_is_not_exposed(self, name):
        with pytest.raises(TaskResultDoesNotExist):
            build_task_result(WORKFLOW_ID, make_status(name=name))

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
        result = build_task_result(WORKFLOW_ID, make_status(output=None, **fields))

        assert ms(result.enqueued_at) == CREATED_AT_MS
        assert ms(result.started_at) == (None if started is None else CREATED_AT_MS + started)
        assert result.last_attempted_at == result.started_at
        assert ms(result.finished_at) == (None if finished is None else CREATED_AT_MS + finished)

    def test_missing_timestamps_and_inputs(self):
        result = build_task_result(
            WORKFLOW_ID, make_status(status='PENDING', created_at=None, dequeued_at=None, input=None, output=None)
        )

        assert result.status == TaskResultStatus.RUNNING
        assert result.enqueued_at is None
        assert result.last_attempted_at is None
        assert result.args == []
        assert result.kwargs == {}

    def test_successful_result_without_output(self):
        result = build_task_result(WORKFLOW_ID, make_status(output=None))

        assert result.status == TaskResultStatus.SUCCESSFUL
        assert result.return_value is None


TIMED_OUT = {'workflow_timeout_ms': 500, 'workflow_deadline_epoch_ms': CREATED_AT_MS + 500}


class TestFailureReasons:
    @pytest.mark.parametrize(
        ('fields', 'cancelled', 'timed_out', 'recovery_exhausted'),
        [
            pytest.param({'status': 'CANCELLED'}, True, False, False, id='cancelled'),
            pytest.param({'status': 'CANCELLED', **TIMED_OUT}, True, True, False, id='timed-out'),
            pytest.param({'status': 'MAX_RECOVERY_ATTEMPTS_EXCEEDED'}, False, False, True, id='recovery-exhausted'),
            pytest.param({'status': 'ERROR', 'error': ValueError('boom')}, False, False, False, id='task-error'),
            pytest.param({'status': 'SUCCESS'}, False, False, False, id='successful'),
            pytest.param({'status': 'PENDING'}, False, False, False, id='running'),
        ],
    )
    def test_reason(self, fields, cancelled, timed_out, recovery_exhausted):
        result = build_task_result(WORKFLOW_ID, make_status(output=None, **fields))

        assert is_cancelled(result) is cancelled
        assert is_timed_out(result) is timed_out
        assert is_recovery_exhausted(result) is recovery_exhausted

    def test_error_class_that_no_longer_exists_is_not_imported(self):
        error = TaskFailure('myapp.errors.RemovedError', 'boom')
        result = build_task_result(WORKFLOW_ID, make_status(status='ERROR', output=None, error=error))

        assert not is_cancelled(result)
        assert not is_timed_out(result)
        assert not is_recovery_exhausted(result)
