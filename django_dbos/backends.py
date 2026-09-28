"""A DBOS-backed backend for Django's tasks framework."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from dbos import error as dbos_error
from django.tasks import TaskResult, TaskResultStatus
from django.tasks.backends.base import BaseTaskBackend
from django.tasks.base import TaskError
from django.tasks.exceptions import TaskResultDoesNotExist

from .client import get_client
from .exceptions import MaxRecoveryAttemptsExceeded, QueueNotDeclared, TaskCancelled, TaskTimedOut
from .logging import logger
from .tasks import DBOSTask
from .tasks_registry import task_from_workflow_name, workflow_name_for_task

if TYPE_CHECKING:
    from dbos import EnqueueOptions

#: DBOS queue priorities run 1 (highest) .. 2**31-1 (lowest). Django priorities
#: run -100 (lowest) .. 100 (highest). Offset so the Django scale fits in the
#: DBOS scale while preserving the order.
_PRIORITY_OFFSET = 101

#: Workflow statuses DBOS uses, mapped onto Django's TaskResultStatus. DBOS
#: has no RUNNING status: a dequeued workflow being executed is PENDING.
_STATUS_MAPPING: dict[str, TaskResultStatus] = {
    'DELAYED': TaskResultStatus.READY,
    'ENQUEUED': TaskResultStatus.READY,
    'PENDING': TaskResultStatus.RUNNING,
    'SUCCESS': TaskResultStatus.SUCCESSFUL,
    'ERROR': TaskResultStatus.FAILED,
    'CANCELLED': TaskResultStatus.FAILED,
    'MAX_RECOVERY_ATTEMPTS_EXCEEDED': TaskResultStatus.FAILED,
}


def _django_to_dbos_priority(django_priority: int) -> int:
    return _PRIORITY_OFFSET - int(django_priority)


def _delay_seconds(run_after: datetime | timedelta | None) -> float | None:
    if run_after is None:
        return None
    if isinstance(run_after, timedelta):
        return max(run_after.total_seconds(), 0.0)
    now = datetime.now(tz=run_after.tzinfo or UTC)
    return max((run_after - now).total_seconds(), 0.0)


class DBOSBackend(BaseTaskBackend):
    """Execute Django tasks durably on DBOS.

    Tasks are enqueued as DBOS workflows on database-backed queues, through a
    ``DBOSClient`` that only talks to the system database; a separate worker
    process (``manage.py dbos worker``) executes them. Results are read back
    from the system database and exposed through the standard ``TaskResult``
    API.
    """

    task_class = DBOSTask
    supports_defer = True
    supports_async_task = True
    supports_get_result = True
    supports_priority = True

    def enqueue(self, task: DBOSTask, args: Any, kwargs: Any) -> TaskResult:
        self.validate_task(task)
        self._validate_queue(task)

        handle = get_client().enqueue(self._enqueue_options(task), *args, **kwargs)
        self._log_enqueued(task, handle.workflow_id)
        try:
            status = handle.get_status()
        except dbos_error.DBOSNonExistentWorkflowError as exc:
            raise TaskResultDoesNotExist(handle.workflow_id) from exc
        return _build_task_result(handle.workflow_id, status)

    async def aenqueue(self, task: DBOSTask, args: Any, kwargs: Any) -> TaskResult:
        self.validate_task(task)
        self._validate_queue(task)

        handle = await get_client().enqueue_async(self._enqueue_options(task), *args, **kwargs)
        self._log_enqueued(task, handle.workflow_id)
        try:
            status = await handle.get_status()
        except dbos_error.DBOSNonExistentWorkflowError as exc:
            raise TaskResultDoesNotExist(handle.workflow_id) from exc
        return _build_task_result(handle.workflow_id, status)

    def get_result(self, result_id: str) -> TaskResult:
        try:
            status = get_client().retrieve_workflow(result_id).get_status()
        except dbos_error.DBOSNonExistentWorkflowError as exc:
            raise TaskResultDoesNotExist(result_id) from exc
        return _build_task_result(result_id, status)

    async def aget_result(self, result_id: str) -> TaskResult:
        try:
            handle = await get_client().retrieve_workflow_async(result_id)
            status = await handle.get_status()
        except dbos_error.DBOSNonExistentWorkflowError as exc:
            raise TaskResultDoesNotExist(result_id) from exc
        return _build_task_result(result_id, status)

    # Internal helpers

    def _enqueue_options(self, task: DBOSTask) -> EnqueueOptions:
        """The DBOS enqueue options for a task: workflow name, queue, priority, delay, params."""
        params = task.dbos_params
        options: dict[str, Any] = {
            'workflow_name': workflow_name_for_task(task),
            'queue_name': task.queue_name,
            'priority': _django_to_dbos_priority(task.priority),
        }
        delay_seconds = _delay_seconds(task.run_after)
        if delay_seconds is not None:
            options['delay_seconds'] = delay_seconds
        if params.deduplication_id is not None:
            options['deduplication_id'] = params.deduplication_id
        if params.workflow_timeout is not None:
            options['workflow_timeout'] = params.workflow_timeout
        return options  # type: ignore[return-value]

    def _validate_queue(self, task: DBOSTask) -> None:
        from . import settings as settings_module

        declared_queues = {queue.name for queue in settings_module.resolve_queues()}
        if task.queue_name not in declared_queues:
            raise QueueNotDeclared(
                f"Queue '{task.queue_name}' is not declared in the DJANGO_DBOS "
                "setting. Declare it under DJANGO_DBOS['QUEUES'] so a worker "
                'can process it.'
            )

    @staticmethod
    def _log_enqueued(task: DBOSTask, workflow_id: str) -> None:
        logger.debug('enqueued task', task_name=task.name, queue=task.queue_name, workflow_id=workflow_id)


def _task_error(error: BaseException) -> TaskError:
    error_class = type(error)
    return TaskError(exception_class_path=f'{error_class.__module__}.{error_class.__qualname__}', traceback=repr(error))


def _cancellation_reason(status: Any) -> TaskCancelled:
    """Why a CANCELLED workflow stopped: DBOS stores no error for it.

    A workflow cancelled by its ``workflow_timeout`` was last updated at or
    after its deadline; any other cancellation came from outside.
    """
    deadline = status.workflow_deadline_epoch_ms
    if deadline is not None and status.updated_at is not None and status.updated_at >= deadline:
        return TaskTimedOut(f'The task exceeded its workflow_timeout of {status.workflow_timeout_ms / 1000:g}s.')
    return TaskCancelled('The task was cancelled.')


def _recovery_exhausted(status: Any) -> MaxRecoveryAttemptsExceeded:
    """Why DBOS gave up on a workflow after repeated crashes: it stores no error for it either."""
    return MaxRecoveryAttemptsExceeded(
        f'The task was interrupted too many times ({status.recovery_attempts} execution or recovery '
        'attempts): DBOS no longer recovers it.'
    )


def _started_at(status: Any) -> datetime | None:
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


def _finished_at(status: Any) -> datetime | None:
    """When a finished task finished: DBOS sets no ``completed_at`` when it gives up recovering it."""
    return _epoch_to_datetime(status.completed_at if status.completed_at is not None else status.updated_at)


def _build_task_result(workflow_id: str, status: Any) -> TaskResult:
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
    elif dbos_status == 'CANCELLED':
        errors = [_task_error(_cancellation_reason(status))]
    elif dbos_status == 'MAX_RECOVERY_ATTEMPTS_EXCEEDED':
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


def _epoch_to_datetime(epoch_ms: int | None) -> datetime | None:
    if epoch_ms is None:
        return None
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)
