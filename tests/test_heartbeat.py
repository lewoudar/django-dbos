"""Unit tests for worker heartbeats (Django database table)."""

import sys
import threading
import types
import uuid
from datetime import timedelta

import pytest
from django.db import DatabaseError
from django.utils import timezone
from structlog.testing import capture_logs

from django_dbos.heartbeat import (
    PURGE_AFTER_SECONDS,
    STALE_AFTER_SECONDS,
    HeartbeatThread,
    delete_heartbeat,
    read_heartbeats,
    worker_id,
    write_heartbeat,
)
from django_dbos.models import WorkerHeartbeat, new_heartbeat_id

pytestmark = pytest.mark.django_db


def _unique_executor() -> str:
    return f'worker-{uuid.uuid4().hex[:8]}'


def test_new_heartbeat_id_is_time_ordered_when_available():
    heartbeat_id = new_heartbeat_id()
    assert isinstance(heartbeat_id, uuid.UUID)
    assert heartbeat_id.version == (7 if hasattr(uuid, 'uuid7') else 4)


class TestWriteAndReadHeartbeat:
    def test_roundtrip(self):
        executor_id, heartbeat_id = _unique_executor(), uuid.uuid4()
        assert write_heartbeat(executor_id, heartbeat_id=heartbeat_id, queues=('default', 'emails')) is True

        (heartbeat,) = read_heartbeats()
        assert heartbeat['id'] == str(heartbeat_id)
        assert heartbeat['worker_id'] == worker_id(executor_id)
        assert heartbeat['executor_id'] == executor_id
        assert heartbeat['queues'] == ['default', 'emails']
        assert heartbeat['has_metrics'] is False
        assert heartbeat['alive'] is True

    def test_rewrite_keeps_started_at(self):
        executor_id, heartbeat_id = _unique_executor(), uuid.uuid4()
        started_at = timezone.now() - timedelta(minutes=5)
        write_heartbeat(executor_id, heartbeat_id=heartbeat_id, started_at=started_at)
        write_heartbeat(executor_id, heartbeat_id=heartbeat_id, started_at=timezone.now())

        row = WorkerHeartbeat.objects.get(id=heartbeat_id)
        assert row.started_at == started_at
        assert WorkerHeartbeat.objects.count() == 1

    def test_each_worker_run_gets_its_own_row(self):
        # Same executor, hostname and pid (a container restarted after a crash).
        executor_id = _unique_executor()
        write_heartbeat(executor_id, heartbeat_id=uuid.uuid4())
        write_heartbeat(executor_id, heartbeat_id=uuid.uuid4())

        assert WorkerHeartbeat.objects.filter(worker_id=worker_id(executor_id)).count() == 2

    def test_stale_heartbeats_are_hidden_by_default(self):
        write_heartbeat(_unique_executor(), heartbeat_id=uuid.uuid4())
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

        write_heartbeat(_unique_executor(), heartbeat_id=uuid.uuid4(), has_metrics=True)
        (heartbeat,) = read_heartbeats()
        assert heartbeat['has_metrics'] is True
        assert heartbeat['cpu_percent'] == 12.5
        assert heartbeat['memory_rss_mb'] == 256.0

    def test_metrics_requested_without_psutil_are_left_empty(self, monkeypatch):
        monkeypatch.setitem(sys.modules, 'psutil', None)  # makes `import psutil` fail

        assert write_heartbeat(_unique_executor(), heartbeat_id=uuid.uuid4(), has_metrics=True) is True
        (heartbeat,) = read_heartbeats()
        assert heartbeat['cpu_percent'] is None
        assert heartbeat['memory_rss_mb'] is None

    def test_delete_removes_own_row_only(self):
        executor_id, mine, other = _unique_executor(), uuid.uuid4(), uuid.uuid4()
        write_heartbeat(executor_id, heartbeat_id=mine)
        write_heartbeat(executor_id, heartbeat_id=other)

        delete_heartbeat(mine)
        assert [row['id'] for row in read_heartbeats()] == [str(other)]

    def test_database_failure_never_raises(self, mocker):
        mocker.patch.object(WorkerHeartbeat.objects, 'update_or_create', side_effect=DatabaseError('db down'))
        assert write_heartbeat(_unique_executor(), heartbeat_id=uuid.uuid4()) is False

    def test_write_and_purges_heartbeats_older_than_a_day(self):
        old, recent = uuid.uuid4(), uuid.uuid4()
        write_heartbeat(_unique_executor(), heartbeat_id=old)
        write_heartbeat(_unique_executor(), heartbeat_id=recent)
        WorkerHeartbeat.objects.filter(id=old).update(
            last_seen_at=timezone.now() - timedelta(seconds=PURGE_AFTER_SECONDS + 60)
        )
        WorkerHeartbeat.objects.filter(id=recent).update(last_seen_at=timezone.now() - timedelta(hours=1))

        write_heartbeat(_unique_executor(), heartbeat_id=uuid.uuid4())

        assert not WorkerHeartbeat.objects.filter(id=old).exists()
        assert WorkerHeartbeat.objects.filter(id=recent).exists()  # stale, but kept for a day

    def test_purge_failure_still_reports_the_heartbeat_written(self, mocker):
        heartbeat_id = uuid.uuid4()
        mocker.patch.object(WorkerHeartbeat.objects, 'filter', side_effect=DatabaseError('db down'))

        with capture_logs() as logs:
            assert write_heartbeat(_unique_executor(), heartbeat_id=heartbeat_id) is True

        assert WorkerHeartbeat.objects.get(id=heartbeat_id)
        assert [log['event'] for log in logs] == ['could not purge stale worker heartbeats']

    def test_delete_failure_never_raises(self, mocker):
        mocker.patch.object(WorkerHeartbeat.objects, 'filter', side_effect=DatabaseError('db down'))
        delete_heartbeat(uuid.uuid4())

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
        heartbeat_id = write.call_args.kwargs['heartbeat_id']
        assert all(call.kwargs['heartbeat_id'] == heartbeat_id for call in write.call_args_list)
        delete.assert_called_once_with(heartbeat_id)

    def test_each_thread_gets_its_own_heartbeat_id(self):
        executor_id = _unique_executor()
        first, second = HeartbeatThread(executor_id=executor_id), HeartbeatThread(executor_id=executor_id)
        assert first._heartbeat_id != second._heartbeat_id

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
