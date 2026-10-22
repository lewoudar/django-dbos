"""Backend tests against a mocked ``DBOSClient`` (no system database).

They cover the async paths, which the integration suite cannot exercise
(see the skipped ``test_async_enqueue_roundtrip``), and the error paths the
SDK only reaches through race conditions.
"""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest
from dbos import error as dbos_error
from django.tasks import TaskResultStatus, task_backends
from django.tasks.exceptions import InvalidTask, TaskResultDoesNotExist

from django_dbos.exceptions import ImproperlyConfiguredDbos
from django_dbos.workflow_names import workflow_name_for_task

from .helpers import CREATED_AT_MS, WORKFLOW_ID, make_status, ms, set_dbos_options
from .tasks import add, async_add


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


class TestDeclaredQueues:
    """The backend accepts exactly the queues a worker declares, whichever way they are declared."""

    @staticmethod
    def backend_queues(settings, **params):
        settings.TASKS = {'dbos': {'BACKEND': 'django_dbos.backends.DBOSBackend', **params}}
        return task_backends['dbos'].queues

    def test_from_options_mapping(self, settings):
        queues = self.backend_queues(
            settings, OPTIONS={'QUEUES': {'emails': None, 'reports': {'worker_concurrency': 1}}}
        )
        assert queues == {'emails', 'reports'}

    def test_from_top_level_list(self, settings):
        assert self.backend_queues(settings, QUEUES=['default', 'emails']) == {'default', 'emails'}

    def test_default_queue_when_none_is_declared(self, settings):
        assert self.backend_queues(settings) == {'default'}

    def test_queues_of_its_own_alias(self, settings):
        # several DBOS backends are reported by django_dbos.E002, yet each one reads its own entry
        settings.TASKS = {
            'first': {'BACKEND': 'django_dbos.backends.DBOSBackend', 'QUEUES': ['first']},
            'second': {'BACKEND': 'django_dbos.backends.DBOSBackend', 'QUEUES': ['second']},
        }
        assert task_backends['second'].queues == {'second'}

    def test_undeclared_queue_is_rejected_at_task_construction(self):
        with pytest.raises(InvalidTask, match="Queue 'ghost' is not valid for backend"):
            replace(add, queue_name='ghost')

    @pytest.mark.parametrize(
        'params',
        [
            pytest.param({'QUEUES': ['default'], 'OPTIONS': {'QUEUES': {'default': None}}}, id='both'),
            pytest.param({'QUEUES': []}, id='any-queue'),
        ],
    )
    def test_misconfigured_queues_raise_on_enqueue_only(self, settings, params):
        # building the backend (task import, Django's checks) must not fail
        settings.TASKS = {**settings.TASKS, 'dbos': {'BACKEND': 'django_dbos.backends.DBOSBackend', **params}}
        with pytest.raises(ImproperlyConfiguredDbos):
            add.enqueue(1, 2)
        with pytest.raises(ImproperlyConfiguredDbos):
            asyncio.run(add.aenqueue(1, 2))

    def test_options_change_resets_the_backend(self, settings):
        set_dbos_options(settings, {'QUEUES': {'emails': None}})
        assert add.get_backend().queues == {'emails'}
