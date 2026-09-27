"""Unit tests for worker heartbeats (Django database table)."""

import sys
import threading
import types
import uuid
from datetime import timedelta

import pytest
from django.db import DatabaseError
from django.utils import timezone

from django_dbos.heartbeat import (
    STALE_AFTER_SECONDS,
    HeartbeatThread,
    delete_heartbeat,
    read_heartbeats,
    worker_id,
    write_heartbeat,
)
from django_dbos.models import WorkerHeartbeat

pytestmark = pytest.mark.django_db


def _unique_executor() -> str:
    return f'worker-{uuid.uuid4().hex[:8]}'


class TestWriteAndReadHeartbeat:
    def test_roundtrip(self):
        executor_id = _unique_executor()
        assert write_heartbeat(executor_id, queues=('default', 'emails')) is True

        (heartbeat,) = read_heartbeats()
        assert heartbeat['worker_id'] == worker_id(executor_id)
        assert heartbeat['executor_id'] == executor_id
        assert heartbeat['queues'] == ['default', 'emails']
        assert heartbeat['has_metrics'] is False
        assert heartbeat['alive'] is True

    def test_rewrite_keeps_started_at(self):
        executor_id = _unique_executor()
        started_at = timezone.now() - timedelta(minutes=5)
        write_heartbeat(executor_id, started_at=started_at)
        write_heartbeat(executor_id, started_at=timezone.now())

        row = WorkerHeartbeat.objects.get(worker_id=worker_id(executor_id))
        assert row.started_at == started_at
        assert WorkerHeartbeat.objects.count() == 1

    def test_stale_heartbeats_are_hidden_by_default(self):
        executor_id = _unique_executor()
        write_heartbeat(executor_id)
        WorkerHeartbeat.objects.update(last_seen_at=timezone.now() - timedelta(seconds=STALE_AFTER_SECONDS + 1))

        assert read_heartbeats() == []
        (stale,) = read_heartbeats(include_stale=True)
        assert stale['alive'] is False

    def test_with_metrics(self, monkeypatch):
        fake_psutil = types.ModuleType('psutil')
        fake_psutil.Process = lambda: types.SimpleNamespace(
            cpu_percent=lambda interval=None: 12.5,
            memory_info=lambda: types.SimpleNamespace(rss=256 * 1024 * 1024),
        )
        monkeypatch.setitem(sys.modules, 'psutil', fake_psutil)

        write_heartbeat(_unique_executor(), has_metrics=True)
        (heartbeat,) = read_heartbeats()
        assert heartbeat['has_metrics'] is True
        assert heartbeat['cpu_percent'] == 12.5
        assert heartbeat['memory_rss_mb'] == 256.0

    def test_metrics_requested_without_psutil_are_left_empty(self, monkeypatch):
        monkeypatch.setitem(sys.modules, 'psutil', None)  # makes `import psutil` fail

        assert write_heartbeat(_unique_executor(), has_metrics=True) is True
        (heartbeat,) = read_heartbeats()
        assert heartbeat['cpu_percent'] is None
        assert heartbeat['memory_rss_mb'] is None

    def test_delete_removes_own_row_only(self):
        mine, other = _unique_executor(), _unique_executor()
        write_heartbeat(mine)
        write_heartbeat(other)
        WorkerHeartbeat.objects.filter(executor_id=other).update(worker_id=f'{other}@elsewhere:1')

        delete_heartbeat(mine)
        assert [row['executor_id'] for row in read_heartbeats()] == [other]

    def test_database_failure_never_raises(self, mocker):
        mocker.patch.object(WorkerHeartbeat.objects, 'update_or_create', side_effect=DatabaseError('db down'))
        assert write_heartbeat(_unique_executor()) is False

    def test_delete_failure_never_raises(self, mocker):
        mocker.patch.object(WorkerHeartbeat.objects, 'filter', side_effect=DatabaseError('db down'))
        delete_heartbeat(_unique_executor())

    def test_missing_table_reads_as_empty(self, mocker):
        mocker.patch.object(WorkerHeartbeat.objects, 'all', side_effect=DatabaseError('no such table'))
        assert read_heartbeats() == []


class TestHeartbeatThread:
    def test_thread_writes_until_stopped_then_cleans_up(self, mocker):
        write = mocker.patch('django_dbos.heartbeat.write_heartbeat')
        delete = mocker.patch('django_dbos.heartbeat.delete_heartbeat')
        executor_id = _unique_executor()

        thread = HeartbeatThread(executor_id=executor_id, queues=('default',), has_metrics=False)
        thread.start()
        thread.stop()  # joins

        assert not thread.is_alive()
        write.assert_called()
        assert write.call_args.kwargs['queues'] == ('default',)
        delete.assert_called_once_with(executor_id)

    def test_thread_keeps_writing_every_interval(self, mocker):
        mocker.patch('django_dbos.heartbeat.HEARTBEAT_INTERVAL_SECONDS', 0.01)
        mocker.patch('django_dbos.heartbeat.delete_heartbeat')
        written = threading.Event()
        write = mocker.patch(
            'django_dbos.heartbeat.write_heartbeat',
            side_effect=lambda *a, **kw: write.call_count >= 3 and written.set(),
        )

        thread = HeartbeatThread(executor_id=_unique_executor())
        thread.start()
        try:
            assert written.wait(timeout=5)
        finally:
            thread.stop()

        assert write.call_count >= 3

    def test_stop_is_idempotent_before_start(self):
        thread = HeartbeatThread(executor_id=_unique_executor())
        thread.stop()  # must not raise although the thread never started

    def test_thread_is_a_named_daemon(self):
        thread = HeartbeatThread(executor_id=_unique_executor())
        assert thread.daemon
        assert thread.name == 'django-dbos-heartbeat'
