"""A DBOS-backed backend for Django's tasks framework."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from dbos import DBOS, SetEnqueueOptions
from dbos._dbos import _get_or_create_dbos_registry
from django.tasks import TaskResult, TaskResultStatus
from django.tasks.backends.base import BaseTaskBackend
from django.tasks.base import TaskError
from django.tasks.exceptions import TaskResultDoesNotExist

from . import dbos_setup
from .exceptions import QueueNotDeclared
from .logging import logger
from .tasks import DBOSTask
from .tasks_registry import register_task_workflow, task_from_workflow_name

#: DBOS queue priorities run 1 (highest) .. 2**31-1 (lowest). Django priorities
#: run -100 (lowest) .. 100 (highest). Offset so the Django scale fits in the
#: DBOS scale while preserving the order.
_PRIORITY_OFFSET = 101

#: Workflow statuses DBOS uses, mapped onto Django's TaskResultStatus.
_STATUS_MAPPING: dict[str, TaskResultStatus] = {
    'DELAYED': TaskResultStatus.READY,
    'ENQUEUED': TaskResultStatus.READY,
    'PENDING': TaskResultStatus.READY,
    'RUNNING': TaskResultStatus.RUNNING,
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

    Tasks are enqueued as DBOS workflows on database-backed queues; a separate
    worker process (``manage.py dbos_worker``) executes them. Results are
    stored in the DBOS system database and exposed through the standard
    ``TaskResult`` API.
    """

    task_class = DBOSTask
    supports_defer = True
    supports_async_task = True
    supports_get_result = True
    supports_priority = True

    def enqueue(self, task: DBOSTask, args: Any, kwargs: Any) -> TaskResult:
        dbos_setup.ensure_initialized()
        self.validate_task(task)

        workflow = self._workflow_for_task(task)
        with self._enqueue_options(task):
            handle = DBOS.enqueue_workflow(task.queue_name, workflow, *args, **kwargs)

        logger.debug(
            'enqueued task',
            task_name=task.name,
            queue=task.queue_name,
            workflow_id=handle.workflow_id,
        )
        return self._fresh_result(handle.workflow_id)

    async def aenqueue(self, task: DBOSTask, args: Any, kwargs: Any) -> TaskResult:
        dbos_setup.ensure_initialized()
        self.validate_task(task)

        workflow = self._workflow_for_task(task)
        with self._enqueue_options(task):
            handle = await DBOS.enqueue_workflow_async(task.queue_name, workflow, *args, **kwargs)

        logger.debug(
            'enqueued task',
            task_name=task.name,
            queue=task.queue_name,
            workflow_id=handle.workflow_id,
        )
        return await self._fresh_result_async(handle.workflow_id)

    def get_result(self, result_id: str) -> TaskResult:
        dbos_setup.ensure_initialized()
        try:
            status = DBOS.retrieve_workflow(result_id).get_status()
        except Exception as exc:  # dbos raises DBOSNonExistentWorkflowError
            raise TaskResultDoesNotExist(result_id) from exc
        return _build_task_result(result_id, status)

    # Internal helpers

    def _workflow_for_task(self, task: DBOSTask) -> Any:
        self._validate_queue(task)
        workflow_name = register_task_workflow(task)
        return _get_workflow(workflow_name)

    def _enqueue_options(self, task: DBOSTask) -> SetEnqueueOptions:
        """Context options applied to the next enqueue in this thread/async context."""
        params = task.dbos_params
        return SetEnqueueOptions(
            priority=_django_to_dbos_priority(task.priority),
            delay_seconds=_delay_seconds(task.run_after),
            **({'deduplication_id': params.deduplication_id} if params.deduplication_id is not None else {}),
            **({'queue_partition_key': params.queue_partition_key} if params.queue_partition_key is not None else {}),
        )

    def _validate_queue(self, task: DBOSTask) -> None:
        from . import settings as settings_module

        declared_queues = {queue.name for queue in settings_module.resolve_queues()}
        if task.queue_name not in declared_queues:
            raise QueueNotDeclared(
                f"Queue '{task.queue_name}' is not declared in the DJANGO_DBOS "
                "setting. Declare it under DJANGO_DBOS['QUEUES'] so a worker "
                'can process it.'
            )

    def _fresh_result(self, workflow_id: str) -> TaskResult:
        try:
            status = DBOS.retrieve_workflow(workflow_id).get_status()
        except Exception as exc:
            raise TaskResultDoesNotExist(workflow_id) from exc
        return _normalize_fresh_result(_build_task_result(workflow_id, status))

    async def _fresh_result_async(self, workflow_id: str) -> TaskResult:
        # Async variant: the synchronous retrieve_workflow() refuses to run
        # inside a running event loop.
        status = await DBOS.get_workflow_status_async(workflow_id)
        if status is None:
            raise TaskResultDoesNotExist(workflow_id)
        return _normalize_fresh_result(_build_task_result(workflow_id, status))


def _get_workflow(workflow_name: str) -> Any:
    workflow = _get_or_create_dbos_registry().workflow_info_map.get(workflow_name)
    if workflow is None:  # pragma: no cover - register_task_workflow just ran
        raise RuntimeError(f'Workflow {workflow_name} was not registered')
    return workflow


def _normalize_fresh_result(result: TaskResult) -> TaskResult:
    """Just-enqueued results: normalize timestamps to the enqueue time."""
    if result.status == TaskResultStatus.READY:
        now = datetime.now(tz=UTC)
        result = replace(result, enqueued_at=now, started_at=None, finished_at=None, last_attempted_at=now)
    return result


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
        error_class = type(status.error)
        errors = [
            TaskError(
                exception_class_path=f'{error_class.__module__}.{error_class.__qualname__}',
                traceback=repr(status.error),
            )
        ]

    inputs = status.input or {}
    enqueued_at = _epoch_to_datetime(status.created_at)
    started_at = enqueued_at if django_status != TaskResultStatus.READY else None
    finished_at = (
        _epoch_to_datetime(status.updated_at)
        if django_status in (TaskResultStatus.SUCCESSFUL, TaskResultStatus.FAILED)
        else None
    )

    result = TaskResult(
        task=task,
        id=workflow_id,
        status=django_status,
        enqueued_at=enqueued_at,
        started_at=started_at,
        finished_at=finished_at,
        last_attempted_at=finished_at or started_at or enqueued_at,
        args=list(inputs.get('args') or []),
        kwargs=dict(inputs.get('kwargs') or {}),
        backend=task.backend,
        errors=errors,
        worker_ids=[],
    )
    if django_status == TaskResultStatus.SUCCESSFUL and status.output is not None:
        object.__setattr__(result, '_return_value', status.output)
    return result


def _epoch_to_datetime(epoch_ms: int | None) -> datetime | None:
    if epoch_ms is None:
        return None
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)
