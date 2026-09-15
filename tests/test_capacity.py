"""Tests for the capacity heuristics and the dbos_status command."""

from __future__ import annotations

import json
import os
import socket
import sys
import types
from datetime import timedelta
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from django.utils import timezone

from django_dbos import capacity
from django_dbos.capacity import QUEUE_WAIT_THRESHOLD_SECONDS, advise_queue
from django_dbos.heartbeat import STALE_AFTER_SECONDS, write_heartbeat
from django_dbos.models import WorkerHeartbeat

from .tasks import add


def _metrics(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        'enqueued': 0,
        'delayed': 0,
        'running': 0,
        'backlog_age_seconds': None,
        'max_queue_wait_ms': None,
        'worker_concurrency': None,
        'global_concurrency': None,
        'advice': 'OK',
    }
    base.update(overrides)
    return base


class TestAdviseQueue:
    @pytest.mark.parametrize(
        ('overrides', 'expected'),
        [
            pytest.param({}, 'SCALE_DOWN', id='idle'),
            pytest.param({'running': 3, 'worker_concurrency': 4}, 'OK', id='running-activity'),
            pytest.param(
                {'enqueued': 10, 'backlog_age_seconds': 3.0, 'running': 2, 'worker_concurrency': 4},
                'OK',
                id='fresh-backlog',
            ),
            pytest.param(
                {
                    'enqueued': 10,
                    'backlog_age_seconds': QUEUE_WAIT_THRESHOLD_SECONDS,
                    'running': 2,
                    'worker_concurrency': 4,
                },
                'OK',
                id='backlog-at-threshold',
            ),
            pytest.param(
                {
                    'enqueued': 10,
                    'backlog_age_seconds': QUEUE_WAIT_THRESHOLD_SECONDS + 1,
                    'running': 2,
                    'worker_concurrency': 4,
                },
                'SCALE_UP',
                id='stale-backlog',
            ),
            pytest.param(
                {'enqueued': 1, 'backlog_age_seconds': 5.0, 'running': 1, 'worker_concurrency': 4},
                'OK',
                id='low-activity',
            ),
        ],
    )
    def test_advise_queue(self, overrides: dict[str, object], expected: str):
        assert advise_queue(_metrics(**overrides)) == expected


def _status(*args: str) -> str:
    """Run dbos_status and return its own output (DBOS logs would interleave on stdout)."""
    out = StringIO()
    call_command('dbos_status', *args, stdout=out)
    return out.getvalue()


# --- Integration (real system database) ---


@pytest.mark.integration
@pytest.mark.django_db
class TestCollect:
    def test_collect_never_launches_dbos(self, dbos_environment, mocker):
        # The report reads the system database through the client: launching
        # DBOS would turn the calling process into a worker.
        ensure_launched = mocker.patch('django_dbos.dbos_setup.ensure_launched')
        capacity.collect()
        ensure_launched.assert_not_called()

    def test_collect_reports_delayed_workflows(self, dbos_environment):
        deferred = add.using(run_after=timezone.now() + timedelta(hours=1))
        deferred.enqueue(1, 2)

        report = capacity.collect(queues=['default'])
        assert report['queues']['default']['delayed'] == 1
        assert report['queues']['default']['advice'] in ('OK', 'SCALE_UP', 'SCALE_DOWN')
        assert report['max_recovery_attempts_exceeded'] == 0

        # JSON output stays parseable
        payload = json.loads(json.dumps(report, default=str))
        assert 'queues' in payload and 'executors' in payload

    def test_collect_filters_queues(self, dbos_environment):
        report = capacity.collect(queues=['emails'])
        assert list(report['queues']) == ['emails']

    def test_worker_heartbeat_visible_in_report(self, dbos_environment):
        write_heartbeat('some-worker', queues=('default',), has_metrics=False)
        report = capacity.collect()
        (worker,) = report['workers']
        assert worker['executor_id'] == 'some-worker'
        assert worker['has_metrics'] is False
        # A worker with a heartbeat but no workflow yet is still an executor.
        assert 'some-worker' in report['executors']

    def test_command_json_output(self, dbos_environment: Any) -> None:
        output = _status('--json')
        payload = json.loads(output)
        assert set(payload) == {
            'queues',
            'executors',
            'workers',
            'max_recovery_attempts_exceeded',
        }
        assert set(payload['queues']) == {'default', 'emails'}

    def test_command_human_output(self, dbos_environment: Any) -> None:
        output = _status()
        assert 'Queue "default"' in output
        assert 'Workers:' in output

    def test_human_output_reports_live_worker_with_metrics(
        self, dbos_environment: Any, monkeypatch: pytest.MonkeyPatch, mocker
    ) -> None:
        fake_psutil = types.ModuleType('psutil')
        fake_psutil.Process = lambda: types.SimpleNamespace(
            cpu_percent=lambda interval=None: 42.0,
            memory_info=lambda: types.SimpleNamespace(rss=1024 * 1024 * 1024),
        )
        monkeypatch.setitem(sys.modules, 'psutil', fake_psutil)
        write_heartbeat('live-worker', queues=('default',), has_metrics=True)
        output = _status()
        assert (
            f'live-worker: {socket.gethostname()} pid {os.getpid()}, queues default, CPU 42.0%, RSS 1024.0 MB' in output
        )

    def test_human_output_reports_live_worker_without_metrics(self, dbos_environment: Any) -> None:
        write_heartbeat('plain-worker', has_metrics=False)
        output = _status()
        assert (
            f'plain-worker: {socket.gethostname()} pid {os.getpid()}, install django-dbos[metrics] for CPU/memory'
            in output
        )

    def test_human_output_flags_executor_without_heartbeat(self, dbos_environment: Any, mocker) -> None:
        # An executor with active workflows but no heartbeat row at all.
        ghost = types.SimpleNamespace(
            status='RUNNING', queue_name='default', executor_id='ghost-worker', created_at=1, dequeued_at=2
        )
        mocker.patch('django_dbos.capacity._active_workflows', return_value=[ghost])
        output = _status()
        assert 'ghost-worker: NO HEARTBEAT (worker down, or django_dbos migrations not applied)' in output

    def test_human_output_flags_stale_heartbeat(self, dbos_environment: Any) -> None:
        write_heartbeat('dead-worker')
        WorkerHeartbeat.objects.update(last_seen_at=timezone.now() - timedelta(seconds=STALE_AFTER_SECONDS + 1))
        output = _status()
        assert f'dead-worker: {socket.gethostname()} pid {os.getpid()}, STALE (last seen' in output

    def test_human_output_with_no_worker_at_all(self, dbos_environment: Any, mocker) -> None:
        # Nothing active, no heartbeat: the section is empty.
        mocker.patch('django_dbos.capacity._active_workflows', return_value=[])
        output = _status()
        assert 'Workers:' in output
        assert '(none)' in output

    def test_human_output_with_queue_filter(self, dbos_environment: Any) -> None:
        output = _status('--queues', 'emails')
        assert 'Queue "emails"' in output
        assert 'Queue "default"' not in output

    def test_json_output_with_queue_filter(self, dbos_environment: Any) -> None:
        output = _status('--json', '--queues', 'emails')
        payload = json.loads(output)
        assert set(payload['queues']) == {'emails'}
