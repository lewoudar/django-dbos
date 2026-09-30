"""Tasks run end to end with the msgspec serializers (real system database)."""

from __future__ import annotations

import pytest
from django.conf import settings
from django.tasks import TaskResultStatus
from django.test import override_settings

from django_dbos import client, dbos_setup

from . import tasks as tasks_module
from .conftest import wait_for_result
from .tasks import add, async_add, durable_context_task, failing_task

pytestmark = pytest.mark.integration


@pytest.fixture(
    scope='module',
    params=['django_dbos.serializers.MsgspecJSONSerializer', 'django_dbos.serializers.MsgspecMsgpackSerializer'],
    ids=['json', 'msgpack'],
)
def msgspec_worker(request, dbos_system):
    """Relaunch the session's DBOS worker with a msgspec serializer, then restore it."""

    def relaunch() -> None:
        dbos_setup.shutdown()
        client.close_client()
        dbos_setup.ensure_launched()

    with override_settings(DJANGO_DBOS={**settings.DJANGO_DBOS, 'SERIALIZER': request.param}):
        relaunch()
        yield request.param
    relaunch()


@pytest.mark.parametrize(
    ('task', 'args', 'expected'),
    [pytest.param(add, (3, 4), 7, id='sync'), pytest.param(async_add, (10, 20), 30, id='async')],
)
def test_task_roundtrip(msgspec_worker, task, args, expected):
    result = wait_for_result(task.enqueue(*args), TaskResultStatus.SUCCESSFUL)
    assert result.return_value == expected
    assert result.args == list(args)


def test_failed_task_reports_the_original_error(msgspec_worker):
    # Only a msgspec serializer keeps the traceback: pickle would report repr(error).
    result = wait_for_result(failing_task.enqueue(), TaskResultStatus.FAILED)

    [error] = result.errors
    assert error.exception_class is ValueError
    assert 'in failing_task' in error.traceback
    assert error.traceback.endswith('ValueError: boom\n')


def test_durable_operations(msgspec_worker):
    tasks_module.results['events'] = []
    result = wait_for_result(durable_context_task.enqueue(), TaskResultStatus.SUCCESSFUL)
    assert result.return_value == {'attempt': 1, 'events': ['durable-context-step']}
