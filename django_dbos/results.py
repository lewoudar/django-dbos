"""Reading a DBOS workflow status back as a Django ``TaskResult``."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from dbos import WorkflowStatusString
from django.tasks import TaskResult, TaskResultStatus
from django.tasks.base import TaskError
from django.tasks.exceptions import TaskResultDoesNotExist

from .exceptions import MaxRecoveryAttemptsExceeded, TaskCancelled, TaskFailure, TaskTimedOut
from .workflow_names import task_from_workflow_name

if TYPE_CHECKING:
    from dbos import WorkflowStatus

# Workflow statuses DBOS uses, mapped onto Django's TaskResultStatus. DBOS
# has no RUNNING status: a dequeued workflow being executed is PENDING.
_STATUS_MAPPING: dict[str, TaskResultStatus] = {
    WorkflowStatusString.DELAYED.value: TaskResultStatus.READY,
    WorkflowStatusString.ENQUEUED.value: TaskResultStatus.READY,
    WorkflowStatusString.PENDING.value: TaskResultStatus.RUNNING,
    WorkflowStatusString.SUCCESS.value: TaskResultStatus.SUCCESSFUL,
    WorkflowStatusString.ERROR.value: TaskResultStatus.FAILED,
    WorkflowStatusString.CANCELLED.value: TaskResultStatus.FAILED,
    WorkflowStatusString.MAX_RECOVERY_ATTEMPTS_EXCEEDED.value: TaskResultStatus.FAILED,
}


def _task_error(error: BaseException) -> TaskError:
    if isinstance(error, TaskFailure):
        # Stored without its class by a safe serializer: report the original class and traceback.
        return TaskError(
            exception_class_path=error.exception_class_path,
            traceback=error.traceback or f'{error.exception_class_path}: {error.message}',
        )
    return TaskError(exception_class_path=_class_path(type(error)), traceback=repr(error))


def _cancellation_reason(status: WorkflowStatus) -> TaskCancelled:
    """Why a CANCELLED workflow stopped: DBOS stores no error for it.

    A workflow cancelled by its ``workflow_timeout`` was last updated at or
    after its deadline; any other cancellation came from outside.
    """
    deadline, timeout_ms = status.workflow_deadline_epoch_ms, status.workflow_timeout_ms
    if (
        deadline is not None
        and timeout_ms is not None
        and status.updated_at is not None
        and status.updated_at >= deadline
    ):
        return TaskTimedOut(f'The task exceeded its workflow_timeout of {timeout_ms / 1000:.15g}s.')
    return TaskCancelled('The task was cancelled.')


def _recovery_exhausted(status: WorkflowStatus) -> MaxRecoveryAttemptsExceeded:
    """Why DBOS gave up on a workflow after repeated crashes: it stores no error for it either."""
    return MaxRecoveryAttemptsExceeded(
        f'The task was interrupted too many times ({status.recovery_attempts} execution or recovery '
        'attempts): DBOS no longer recovers it.'
    )


def _started_at(status: WorkflowStatus) -> datetime | None:
    """When the task started, or ``None`` if it never did.

    ``dequeued_at`` is when a worker took the workflow from its queue (its
    latest dequeue, after a recovery). A started workflow without one either
    never went through a queue — a cron firing runs the task as a child
    started on the spot — or was cancelled, which clears it: its creation time
    is the best estimate left. ``recovery_attempts`` counts the executions, so
    it tells whether the workflow started at all.
    """
    if status.dequeued_at is not None:
        return _epoch_to_datetime(status.dequeued_at)
    if status.recovery_attempts:
        return _epoch_to_datetime(status.created_at)
    return None


def _finished_at(status: WorkflowStatus) -> datetime | None:
    """When a finished task finished: DBOS sets no ``completed_at`` when it gives up recovering it."""
    return _epoch_to_datetime(status.completed_at if status.completed_at is not None else status.updated_at)


def build_task_result(workflow_id: str, status: WorkflowStatus) -> TaskResult:
    dbos_status = status.status
    if dbos_status not in _STATUS_MAPPING:
        raise TaskResultDoesNotExist(workflow_id)

    task = task_from_workflow_name(status.name or '')
    if task is None:
        raise TaskResultDoesNotExist(workflow_id)

    django_status = _STATUS_MAPPING[dbos_status]
    errors = []
    if status.error is not None:
        errors = [_task_error(status.error)]
    elif dbos_status == WorkflowStatusString.CANCELLED.value:
        errors = [_task_error(_cancellation_reason(status))]
    elif dbos_status == WorkflowStatusString.MAX_RECOVERY_ATTEMPTS_EXCEEDED.value:
        errors = [_task_error(_recovery_exhausted(status))]

    inputs = status.input or {}
    started_at = _started_at(status)
    finished_at = (
        _finished_at(status) if django_status in (TaskResultStatus.SUCCESSFUL, TaskResultStatus.FAILED) else None
    )

    result = TaskResult(
        task=task,
        id=workflow_id,
        status=django_status,
        enqueued_at=_epoch_to_datetime(status.created_at),
        started_at=started_at,
        finished_at=finished_at,
        # DBOS only keeps the latest dequeue: it is the start of the last attempt too.
        last_attempted_at=started_at,
        args=list(inputs.get('args') or []),
        kwargs=dict(inputs.get('kwargs') or {}),
        backend=task.backend,
        errors=errors,
        # Django counts attempts as len(worker_ids). DBOS counts executions
        # (recovery_attempts) but only keeps the latest executor.
        worker_ids=[status.executor_id or ''] * (status.recovery_attempts or 0),
    )
    if django_status == TaskResultStatus.SUCCESSFUL and status.output is not None:
        object.__setattr__(result, '_return_value', status.output)
    return result


def is_cancelled(result: TaskResult) -> bool:
    """Whether the task was cancelled, from the admin, the client or its ``workflow_timeout``.

    Reads the result as it is: call ``result.refresh()`` first for the latest state.

    Args:
        result: A task result, from any backend.

    Returns:
        ``True`` if the result is ``FAILED`` with ``TaskCancelled`` or its
        subclass ``TaskTimedOut``; ``False`` otherwise, including for results
        of other backends.
    """
    return _failed_with(result, TaskCancelled, TaskTimedOut)


def is_timed_out(result: TaskResult) -> bool:
    """Whether the task was cancelled because it exceeded its ``workflow_timeout``.

    Reads the result as it is: call ``result.refresh()`` first for the latest state.

    Args:
        result: A task result, from any backend.

    Returns:
        ``True`` if the result is ``FAILED`` with ``TaskTimedOut``; ``False``
        otherwise, including for results of other backends.
    """
    return _failed_with(result, TaskTimedOut)


def is_recovery_exhausted(result: TaskResult) -> bool:
    """Whether DBOS gave up recovering the task after more worker crashes than ``max_recovery_attempts`` allows.

    Reads the result as it is: call ``result.refresh()`` first for the latest state.

    Args:
        result: A task result, from any backend.

    Returns:
        ``True`` if the result is ``FAILED`` with ``MaxRecoveryAttemptsExceeded``;
        ``False`` otherwise, including for results of other backends.
    """
    return _failed_with(result, MaxRecoveryAttemptsExceeded)


def _failed_with(result: TaskResult, *error_classes: type[BaseException]) -> bool:
    """Whether the error that ended the task is one of ``error_classes``.

    Compares class paths: ``TaskError.exception_class`` imports the class, and
    a task error whose class no longer exists would raise ``ImportError``.
    """
    if result.status != TaskResultStatus.FAILED or not result.errors:
        return False
    return result.errors[-1].exception_class_path in {_class_path(error_class) for error_class in error_classes}


def _class_path(error_class: type[BaseException]) -> str:
    return f'{error_class.__module__}.{error_class.__qualname__}'


def _epoch_to_datetime(epoch_ms: int | None) -> datetime | None:
    if epoch_ms is None:
        return None
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)
