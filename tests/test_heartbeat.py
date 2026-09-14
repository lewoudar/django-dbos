"""Unit tests for worker heartbeats (cache-based)."""

import sys
import types
import uuid

from django.core.cache import cache

from django_dbos.heartbeat import (
    HeartbeatThread,
    read_heartbeats,
    write_heartbeat,
)


def _unique_executor() -> str:
    # LocMemCache is per-process but shared between tests: isolate each test.
    return f'worker-{uuid.uuid4().hex[:8]}'


class TestWriteAndReadHeartbeat:
    def test_roundtrip(self):
        executor_id = _unique_executor()
        assert write_heartbeat(executor_id, has_metrics=False) is True
        heartbeats = read_heartbeats([executor_id])
        assert heartbeats[executor_id]['executor_id'] == executor_id
        assert heartbeats[executor_id]['has_metrics'] is False

    def test_missing_executor_is_omitted(self):
        assert read_heartbeats([_unique_executor()]) == {}

    def test_with_metrics(self, monkeypatch):
        fake_psutil = types.ModuleType('psutil')
        fake_psutil.Process = lambda: types.SimpleNamespace(
            cpu_percent=lambda interval=None: 12.5,
            memory_info=lambda: types.SimpleNamespace(rss=256 * 1024 * 1024),
            pid=4242,
        )
        monkeypatch.setitem(sys.modules, 'psutil', fake_psutil)

        executor_id = _unique_executor()
        write_heartbeat(executor_id, has_metrics=True)
        heartbeat = read_heartbeats([executor_id])[executor_id]
        assert heartbeat['pid'] == 4242
        assert heartbeat['cpu_percent'] == 12.5
        assert heartbeat['memory_rss_mb'] == 256.0

    def test_cache_failure_never_raises(self, mocker):
        executor_id = _unique_executor()
        mocker.patch.object(cache, 'set', side_effect=RuntimeError('cache down'))
        assert write_heartbeat(executor_id, has_metrics=False) is False


class TestHeartbeatThread:
    def test_thread_writes_until_stopped(self):
        executor_id = _unique_executor()
        thread = HeartbeatThread(executor_id=executor_id, has_metrics=False)
        thread.start()
        thread.stop()  # joins
        assert not thread.is_alive()
        assert read_heartbeats([executor_id]), 'heartbeat key should survive the worker stop'

    def test_stop_is_idempotent_before_start(self):
        thread = HeartbeatThread(executor_id=_unique_executor(), has_metrics=False)
        thread.stop()  # must not raise although the thread never started

    def test_thread_is_a_named_daemon(self):
        thread = HeartbeatThread(executor_id=_unique_executor(), has_metrics=False)
        assert thread.daemon
        assert thread.name == 'django-dbos-heartbeat'
