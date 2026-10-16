"""Tests for the purge of the workflow history."""

from __future__ import annotations

import types
from datetime import datetime, timedelta

import pytest
from django.tasks import TaskResultStatus
from django.utils import timezone

from django_dbos import purge_workflows
from django_dbos.client import get_client
from django_dbos.retention import TERMINAL_STATUSES

from .conftest import wait_for_result
from .tasks import add


def _workflows(*ids: str) -> list[types.SimpleNamespace]:
    return [types.SimpleNamespace(workflow_id=workflow_id) for workflow_id in ids]


class TestPurgeWorkflows:
    def test_deletes_batches_until_none_is_left(self, mocker):
        client = mocker.patch('django_dbos.retention.get_client').return_value
        client.list_workflows.side_effect = [_workflows('a', 'b'), _workflows('c'), []]

        assert purge_workflows(timedelta(days=30), batch_size=2) == 3
        assert client.delete_workflows.call_args_list == [mocker.call(['a', 'b']), mocker.call(['c'])]

    def test_lists_finished_workflows_completed_before_the_cutoff(self, mocker):
        client = mocker.patch('django_dbos.retention.get_client').return_value
        client.list_workflows.return_value = []
        before = timezone.now()

        assert purge_workflows(timedelta(days=7), batch_size=10) == 0

        client.delete_workflows.assert_not_called()
        kwargs = client.list_workflows.call_args.kwargs
        cutoff = datetime.fromisoformat(kwargs.pop('completed_before'))
        assert before - timedelta(days=7) <= cutoff <= timezone.now() - timedelta(days=7)
        assert kwargs == {
            'status': list(TERMINAL_STATUSES),
            'limit': 10,
            'load_input': False,
            'load_output': False,
        }

    @pytest.mark.parametrize('older_than', [timedelta(0), timedelta(seconds=-1)])
    def test_rejects_a_non_positive_age(self, older_than, mocker):
        client = mocker.patch('django_dbos.retention.get_client')
        with pytest.raises(ValueError, match='older_than must be a positive duration'):
            purge_workflows(older_than)

        client.assert_not_called()

    @pytest.mark.parametrize('batch_size', [0, -1])
    def test_rejects_a_non_positive_batch_size(self, batch_size, mocker):
        client = mocker.patch('django_dbos.retention.get_client')
        with pytest.raises(ValueError, match='batch_size must be a positive integer'):
            purge_workflows(timedelta(days=1), batch_size=batch_size)

        client.assert_not_called()


@pytest.mark.integration
@pytest.mark.django_db
class TestPurgeWorkflowsIntegration:
    def test_purges_finished_workflows_and_keeps_active_ones(self, dbos_environment):
        finished = [wait_for_result(add.enqueue(i, i), TaskResultStatus.SUCCESSFUL) for i in range(2)]
        # Not finished yet: a purge, however aggressive, must leave it alone.
        deferred = add.using(run_after=timezone.now() + timedelta(hours=1)).enqueue(1, 2)

        # Not an equality: the system database is shared by the whole session, so
        # the purge also takes the workflows of earlier tests and the firings of
        # the every-second cron task.
        assert purge_workflows(timedelta(microseconds=1), batch_size=1) >= len(finished)

        ids = [result.id for result in (*finished, deferred)]
        remaining = get_client().list_workflows(workflow_ids=ids, load_input=False, load_output=False)
        assert [workflow.workflow_id for workflow in remaining] == [deferred.id]
