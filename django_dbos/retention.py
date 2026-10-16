"""Retention of the workflow history kept in the DBOS system database.

DBOS records every workflow (status, inputs, output, steps) and never deletes
them: the history grows without bound. ``purge_workflows`` deletes finished
workflows past a given age; run it periodically, typically from a ``@cron``
task. It goes through the client (system database only), so it works from any
process, the worker included.

Worker heartbeats need no such purge: each worker run updates a single row,
and stale rows are deleted by the next heartbeat write (see ``heartbeat.py``).
"""

from __future__ import annotations

from datetime import timedelta

from dbos import WorkflowStatusString
from django.utils import timezone

from .client import get_client
from .logging import logger

# Workflows in these statuses are over for good. MAX_RECOVERY_ATTEMPTS_EXCEEDED
# is left alone: those are dead letters, to inspect or resume from the admin.
TERMINAL_STATUSES = (
    WorkflowStatusString.SUCCESS.value,
    WorkflowStatusString.ERROR.value,
    WorkflowStatusString.CANCELLED.value,
)


def purge_workflows(older_than: timedelta, *, batch_size: int = 1000) -> int:
    """Delete the finished workflows completed more than ``older_than`` ago.

    Only ``SUCCESS``, ``ERROR`` and ``CANCELLED`` workflows are deleted;
    children are not deleted along with their parent (a child still running
    must survive), they are purged on their own once finished.

    Args:
        older_than: Minimum time elapsed since a workflow completed.
        batch_size: Number of workflows listed and deleted per round trip.

    Returns:
        The number of workflows deleted.

    Raises:
        ValueError: ``older_than`` or ``batch_size`` is not positive.
    """
    if older_than <= timedelta(0):
        raise ValueError(f'older_than must be a positive duration, got {older_than!r}')
    if batch_size <= 0:
        raise ValueError(f'batch_size must be a positive integer, got {batch_size!r}')

    client = get_client()
    cutoff = (timezone.now() - older_than).isoformat()
    deleted = 0
    while True:
        workflows = client.list_workflows(
            status=list(TERMINAL_STATUSES),
            completed_before=cutoff,
            limit=batch_size,
            load_input=False,
            load_output=False,
        )
        if not workflows:
            break
        client.delete_workflows([workflow.workflow_id for workflow in workflows])
        deleted += len(workflows)

    logger.info('workflows purged', deleted=deleted, older_than=str(older_than))
    return deleted
