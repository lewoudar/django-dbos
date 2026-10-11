"""A DBOS-backed backend for Django's tasks framework."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from dbos import error as dbos_error
from django.tasks import TaskResult
from django.tasks.backends.base import BaseTaskBackend
from django.tasks.exceptions import TaskResultDoesNotExist

from . import settings as settings_module
from .client import get_client
from .exceptions import QueueNotDeclared
from .logging import logger
from .results import build_task_result
from .tasks import DBOSTask
from .workflow_names import workflow_name_for_task

if TYPE_CHECKING:
    from dbos import EnqueueOptions

# DBOS queue priorities run 1 (highest) .. 2**31-1 (lowest). Django priorities
# run -100 (lowest) .. 100 (highest). Offset so the Django scale fits in the
# DBOS scale while preserving the order.
_PRIORITY_OFFSET = 101


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
        return build_task_result(handle.workflow_id, status)

    async def aenqueue(self, task: DBOSTask, args: Any, kwargs: Any) -> TaskResult:
        self.validate_task(task)
        self._validate_queue(task)

        handle = await get_client().enqueue_async(self._enqueue_options(task), *args, **kwargs)
        self._log_enqueued(task, handle.workflow_id)
        try:
            status = await handle.get_status()
        except dbos_error.DBOSNonExistentWorkflowError as exc:
            raise TaskResultDoesNotExist(handle.workflow_id) from exc
        return build_task_result(handle.workflow_id, status)

    def get_result(self, result_id: str) -> TaskResult:
        try:
            status = get_client().retrieve_workflow(result_id).get_status()
        except dbos_error.DBOSNonExistentWorkflowError as exc:
            raise TaskResultDoesNotExist(result_id) from exc
        return build_task_result(result_id, status)

    async def aget_result(self, result_id: str) -> TaskResult:
        try:
            handle = await get_client().retrieve_workflow_async(result_id)
            status = await handle.get_status()
        except dbos_error.DBOSNonExistentWorkflowError as exc:
            raise TaskResultDoesNotExist(result_id) from exc
        return build_task_result(result_id, status)

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
