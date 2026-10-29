"""A DBOS-backed backend for Django's tasks framework."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from dbos import error as dbos_error
from django.tasks import TaskResult
from django.tasks.backends.base import BaseTaskBackend
from django.tasks.base import TASK_MAX_PRIORITY, TASK_MIN_PRIORITY
from django.tasks.exceptions import InvalidTask, TaskResultDoesNotExist

from . import settings as settings_module
from .client import get_client
from .exceptions import ImproperlyConfiguredDbos
from .logging import logger
from .params import enqueue_signature, resolve_deduplication_id, resolve_partition_key
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
    # Django's validate_task accepts any whole number, such as 5.0; DBOS expects an int.
    return _PRIORITY_OFFSET - int(django_priority)


def _dbos_to_django_priority(dbos_priority: int | None) -> int | None:
    # Workflows not enqueued through the backend (cron runs, direct starts)
    # carry 0 or None: they have no Django priority.
    if dbos_priority is None:
        return None

    django_priority = _PRIORITY_OFFSET - dbos_priority
    if TASK_MIN_PRIORITY <= django_priority <= TASK_MAX_PRIORITY:
        return django_priority

    return None


def _delay_seconds(run_after: datetime | None) -> float | None:
    if run_after is None:
        return None
    # With USE_TZ = False, Django expects a naive datetime in local time, as timezone.now() returns it.
    if run_after.tzinfo is None:
        run_after = run_after.astimezone()
    return max((run_after - datetime.now(tz=UTC)).total_seconds(), 0.0)


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

    def __init__(self, alias: str, params: dict[str, Any]) -> None:
        super().__init__(alias, params)
        # Tasks may only target the queues a worker declares. A misconfiguration
        # is reported by django_dbos.E001 and raised on enqueue, not here: the
        # backend is built while importing task modules and running checks.
        self._queues_error: str | None = None
        self._partitioned_queues: set[str] = set()
        try:
            queues = settings_module.resolve_queues(alias)
        except ImproperlyConfiguredDbos as exc:
            self._queues_error = str(exc)
        else:
            self.queues = {queue.name for queue in queues}
            self._partitioned_queues = {queue.name for queue in queues if queue.partitioned}

    def validate_task(self, task: DBOSTask) -> None:
        # Task.using(backend=...) keeps the task's class: a task declared for
        # another backend never becomes a DBOSTask, and carries no dbos_params.
        if not isinstance(task, DBOSTask):
            raise InvalidTask(
                f"Task '{task.name}' was declared for another backend: declare it with "
                f'@task(backend={self.alias!r}) instead of moving it with .using(backend=...).'
            )
        super().validate_task(task)
        partition_key = task.dbos_params.partition_key
        if isinstance(partition_key, str) and partition_key not in enqueue_signature(task).parameters:
            raise InvalidTask(f"partition_key {partition_key!r} is not an argument of task '{task.name}'.")

    def is_partitioned(self, queue_name: str) -> bool:
        """Whether a queue of this backend is partitioned: its enqueues need a partition key.

        Args:
            queue_name: The queue's name.

        Returns:
            ``True`` when the queue declares a ``partition_*`` limit.
        """
        return queue_name in self._partitioned_queues

    def enqueue(self, task: DBOSTask, args: Any, kwargs: Any) -> TaskResult:
        self._validate_queues_config()
        self.validate_task(task)

        handle = get_client().enqueue(self._enqueue_options(task, args, kwargs), *args, **kwargs)
        self._log_enqueued(task, handle.workflow_id)
        try:
            status = handle.get_status()
        except dbos_error.DBOSNonExistentWorkflowError as exc:
            raise TaskResultDoesNotExist(handle.workflow_id) from exc
        return build_task_result(handle.workflow_id, status)

    async def aenqueue(self, task: DBOSTask, args: Any, kwargs: Any) -> TaskResult:
        self._validate_queues_config()
        self.validate_task(task)

        handle = await get_client().enqueue_async(self._enqueue_options(task, args, kwargs), *args, **kwargs)
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

    def _enqueue_options(self, task: DBOSTask, args: Any, kwargs: Any) -> EnqueueOptions:
        """The DBOS enqueue options for a task: workflow name, queue, priority, delay, params, partition."""
        params = task.dbos_params
        options: dict[str, Any] = {
            'workflow_name': workflow_name_for_task(task),
            'queue_name': task.queue_name,
            'priority': _django_to_dbos_priority(task.priority),
        }
        delay_seconds = _delay_seconds(task.run_after)
        if delay_seconds is not None:
            options['delay_seconds'] = delay_seconds
        deduplication_id = resolve_deduplication_id(task, args, kwargs)
        if deduplication_id is not None:
            options['deduplication_id'] = deduplication_id
        if params.workflow_timeout is not None:
            options['workflow_timeout'] = params.workflow_timeout
        if self.is_partitioned(task.queue_name):
            # DBOSClient checks nothing: without a key, the execution would stay enqueued forever.
            options['queue_partition_key'] = self._partition_key(task, args, kwargs)
        return options  # type: ignore[return-value]

    @staticmethod
    def _partition_key(task: DBOSTask, args: Any, kwargs: Any) -> str:
        if task.dbos_params.deduplication_id is not None:
            raise ImproperlyConfiguredDbos(
                f"Task '{task.name}' has a deduplication_id, which DBOS does not support on the "
                f"partitioned queue '{task.queue_name}'."
            )
        key = resolve_partition_key(task, args, kwargs)
        if key is None:
            raise ImproperlyConfiguredDbos(
                f"Queue '{task.queue_name}' is partitioned: task '{task.name}' needs "
                '@dbos_params(partition_key=...) to be enqueued there.'
            )
        return key

    def _validate_queues_config(self) -> None:
        if self._queues_error is not None:
            raise ImproperlyConfiguredDbos(self._queues_error)

    @staticmethod
    def _log_enqueued(task: DBOSTask, workflow_id: str) -> None:
        logger.debug('enqueued task', task_name=task.name, queue=task.queue_name, workflow_id=workflow_id)
