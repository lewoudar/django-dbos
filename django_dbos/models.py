"""Models of django-dbos: worker heartbeats.

DBOS keeps every workflow-related fact in its system database; what it does
not know is which worker processes are *alive right now* — an idle worker
leaves no trace there. Workers therefore push a heartbeat row into the
Django database (see ``heartbeat.py``), which any process with the ORM can
read: the ``dbos status`` command, the admin, an autoscaling script.
"""

from __future__ import annotations

import uuid

from django.db import models


def new_heartbeat_id() -> uuid.UUID:
    """UUIDv7 (time-ordered) on Python 3.14+, UUIDv4 before."""
    return getattr(uuid, 'uuid7', uuid.uuid4)()


class WorkerHeartbeat(models.Model):
    """Liveness and load of one run of a ``dbos worker`` process.

    ``id`` is generated once per worker run, so a worker restarted with the
    same hostname and pid (pid 1 in a container) gets a fresh row.
    ``executor_id`` is the identity DBOS records on workflow rows (several
    processes may share it, e.g. the default ``"local"``); ``worker_id``
    (``executor_id@hostname:pid``) is a human-readable label.
    """

    id = models.UUIDField(primary_key=True, default=new_heartbeat_id, editable=False)
    worker_id = models.CharField(max_length=255, db_index=True)
    executor_id = models.CharField(max_length=255, db_index=True)
    hostname = models.CharField(max_length=255)
    pid = models.IntegerField()
    queues = models.JSONField(default=list, blank=True)
    has_metrics = models.BooleanField(default=False)
    cpu_percent = models.FloatField(null=True, blank=True)
    memory_rss_mb = models.FloatField(null=True, blank=True)
    started_at = models.DateTimeField()
    last_seen_at = models.DateTimeField(db_index=True)

    class Meta:
        verbose_name = 'worker heartbeat'
        verbose_name_plural = 'worker heartbeats'
        ordering = ('executor_id', 'hostname', 'pid')

    def __str__(self) -> str:
        return self.worker_id
