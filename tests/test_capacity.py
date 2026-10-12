"""Tests for the capacity heuristics and the dbos status command."""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import types
import uuid
from datetime import timedelta
from io import StringIO
from typing import Any

import pytest
from click.exceptions import Exit
from django.core.management import call_command
from django.utils import timezone
from sqlalchemy.exc import OperationalError

from django_dbos import capacity
from django_dbos.capacity import DEFAULT_SCALE_UP_BACKLOG_AGE_SECONDS, advise_queue
from django_dbos.exceptions import ImproperlyConfiguredDbos
from django_dbos.heartbeat import STALE_AFTER_SECONDS, write_heartbeat
from django_dbos.models import WorkerHeartbeat
from django_dbos.settings import resolve_capacity, resolve_queues, resolve_settings

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
                    'backlog_age_seconds': DEFAULT_SCALE_UP_BACKLOG_AGE_SECONDS,
                    'running': 2,
                    'worker_concurrency': 4,
                },
                'OK',
                id='backlog-at-threshold',
            ),
            pytest.param(
                {
                    'enqueued': 10,
                    'backlog_age_seconds': DEFAULT_SCALE_UP_BACKLOG_AGE_SECONDS + 1,
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


def status(*args: str) -> str:
    """Run `dbos status` and return its own output (DBOS logs would interleave on real stdout).

    django-click (2.5.0, the latest released version) does not yet support
    `call_command(..., stdout=...)` (its `base_stealth_options` is empty, so
    Django's own call_command rejects the kwarg) - redirect sys.stdout ourselves.
    """
    out = StringIO()
    with contextlib.redirect_stdout(out):
        call_command('dbos', 'status', *args)
    return out.getvalue()


class TestConfigurableThresholds:
    def test_explicit_thresholds_win(self):
        metrics = _metrics(enqueued=1, backlog_age_seconds=10.0, running=3)
        assert advise_queue(metrics) == 'OK'
        assert advise_queue(metrics, scale_up_backlog_age_seconds=5) == 'SCALE_UP'
        assert advise_queue(_metrics(running=2), scale_down_max_running=2) == 'SCALE_DOWN'

    def test_thresholds_come_from_settings(self, settings):
        settings.DJANGO_DBOS = {**settings.DJANGO_DBOS, 'CAPACITY': {'SCALE_UP_BACKLOG_AGE_SECONDS': 5}}
        assert advise_queue(_metrics(enqueued=1, backlog_age_seconds=10.0, running=1)) == 'SCALE_UP'

    @pytest.mark.parametrize(
        ('capacity', 'message'),
        [
            pytest.param({'SCALE_UP_BACKLOG_AGE_SECOND': 5}, 'unknown threshold', id='unknown-key'),
            pytest.param({'SCALE_UP_BACKLOG_AGE_SECONDS': -1}, 'non-negative number', id='negative'),
            pytest.param({'SCALE_DOWN_MAX_RUNNING': True}, 'non-negative number', id='bool'),
            pytest.param({'ADVISOR': 'nowhere.advise'}, 'ADVISOR', id='unknown-advisor-path'),
            pytest.param({'ADVISOR': 42}, 'callable', id='advisor-not-callable'),
            pytest.param([], 'must be a mapping', id='not-a-mapping'),
        ],
    )
    def test_invalid_capacity_setting(self, settings, capacity, message):
        settings.DJANGO_DBOS = {**settings.DJANGO_DBOS, 'CAPACITY': capacity}
        with pytest.raises(ImproperlyConfiguredDbos, match=message):
            resolve_capacity()

    def test_invalid_per_queue_thresholds(self, settings):
        settings.DJANGO_DBOS = {**settings.DJANGO_DBOS, 'QUEUES': {'emails': {'capacity': {'ADVISOR': 'x'}}}}
        with pytest.raises(ImproperlyConfiguredDbos, match=r"QUEUES'\]\['emails'\]\['capacity'\]: unknown threshold"):
            resolve_queues()

    def test_per_queue_thresholds_override_global_ones(self, settings):
        settings.DJANGO_DBOS = {
            **settings.DJANGO_DBOS,
            'CAPACITY': {'SCALE_UP_BACKLOG_AGE_SECONDS': 30},
            'QUEUES': {'default': None, 'emails': {'capacity': {'SCALE_UP_BACKLOG_AGE_SECONDS': 300}}},
        }
        resolved = resolve_settings()
        default, emails = resolved.queues
        assert resolved.capacity.thresholds_for(default) == {
            'scale_up_backlog_age_seconds': 30,
            'scale_down_max_running': 0,
        }
        assert resolved.capacity.thresholds_for(emails)['scale_up_backlog_age_seconds'] == 300


def custom_advisor(queue_name: str, metrics: dict[str, object], thresholds: dict[str, object]) -> str:
    return f'CUSTOM:{queue_name}:{thresholds["scale_up_backlog_age_seconds"]}'


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

    def test_queue_less_child_workflows_count_for_executors_only(self, dbos_environment, mocker):
        # A task workflow run inline by a cron parent has no queue of its own.
        child = types.SimpleNamespace(
            status='PENDING', queue_name=None, executor_id='worker-1', created_at=1, dequeued_at=None
        )
        mocker.patch('django_dbos.capacity._active_workflows', return_value=[child])
        report = capacity.collect()
        assert set(report['queues']) == {'default', 'emails'}
        assert report['executors']['worker-1'] == {'running': 1, 'enqueued': 0}

    def test_collect_tolerates_partial_workflow_timestamps(self, dbos_environment, mocker):
        now_ms = timezone.now().timestamp() * 1000

        def workflow(status, **fields):
            values = {'queue_name': 'default', 'executor_id': None, 'created_at': None, 'dequeued_at': None}
            return types.SimpleNamespace(status=status, **(values | fields))

        mocker.patch(
            'django_dbos.capacity._active_workflows',
            return_value=[
                workflow('ENQUEUED', created_at=now_ms - 120_000),  # oldest waiting
                workflow('ENQUEUED', created_at=now_ms - 10_000),  # younger: does not lower the age
                workflow('ENQUEUED'),  # no created_at: counted, no age
                workflow('PENDING'),  # executing, never dequeued: no queue wait
                workflow('PENDING', created_at=now_ms - 5_000, dequeued_at=now_ms - 4_500),
                workflow('SUCCESS'),  # not an active status: ignored
            ],
        )

        metrics = capacity.collect(queues=['default'])['queues']['default']

        assert metrics['enqueued'] == 3
        assert metrics['running'] == 2
        assert 119 < metrics['backlog_age_seconds'] < 180
        assert metrics['max_queue_wait_ms'] == 500

    def test_collect_filters_queues(self, dbos_environment):
        report = capacity.collect(queues=['emails'])
        assert list(report['queues']) == ['emails']

    def test_collect_reports_the_thresholds_applied_per_queue(self, dbos_environment, settings):
        settings.DJANGO_DBOS = {
            **settings.DJANGO_DBOS,
            'QUEUES': {'default': None, 'emails': {'capacity': {'SCALE_UP_BACKLOG_AGE_SECONDS': 300}}},
        }
        report = capacity.collect()
        assert report['queues']['default']['thresholds']['scale_up_backlog_age_seconds'] == 60.0
        assert report['queues']['emails']['thresholds']['scale_up_backlog_age_seconds'] == 300

    def test_collect_uses_the_configured_advisor(self, dbos_environment, settings):
        settings.DJANGO_DBOS = {**settings.DJANGO_DBOS, 'CAPACITY': {'ADVISOR': 'tests.test_capacity.custom_advisor'}}
        report = capacity.collect(queues=['emails'])
        assert report['queues']['emails']['advice'] == 'CUSTOM:emails:60.0'

    def test_collect_advisor_argument_wins(self, dbos_environment, settings):
        settings.DJANGO_DBOS = {**settings.DJANGO_DBOS, 'CAPACITY': {'ADVISOR': 'tests.test_capacity.custom_advisor'}}
        report = capacity.collect(queues=['emails'], advisor=lambda name, metrics, thresholds: 'MINE')
        assert report['queues']['emails']['advice'] == 'MINE'

    def test_worker_heartbeat_visible_in_report(self, dbos_environment):
        write_heartbeat('some-worker', heartbeat_id=uuid.uuid4(), queues=('default',), has_metrics=False)
        report = capacity.collect()
        (worker,) = report['workers']
        assert worker['executor_id'] == 'some-worker'
        assert worker['has_metrics'] is False
        # A worker with a heartbeat but no workflow yet is still an executor.
        assert 'some-worker' in report['executors']

    def test_command_json_output(self, dbos_environment: Any) -> None:
        output = status('--json')
        payload = json.loads(output)
        assert set(payload) == {
            'queues',
            'executors',
            'workers',
            'max_recovery_attempts_exceeded',
        }
        assert set(payload['queues']) == {'default', 'emails'}

    def test_command_human_output(self, dbos_environment: Any) -> None:
        output = status()
        assert 'Queue "default"' in output
        assert 'Workers:' in output

    def test_human_output_reports_waits_and_exhausted_recoveries(self, dbos_environment: Any, mocker) -> None:
        collect = capacity.collect

        def degraded_report(**kwargs: Any) -> dict[str, Any]:
            report = collect(**kwargs)
            report['queues']['default'] |= {'backlog_age_seconds': 42.4, 'max_queue_wait_ms': 1500}
            report['max_recovery_attempts_exceeded'] = 2
            return report

        mocker.patch.object(capacity, 'collect', side_effect=degraded_report)
        output = status()
        assert 'Oldest waiting workflow: 42s' in output
        assert 'Max queue wait (running): 1500 ms' in output
        assert '2 workflow(s) exhausted their recovery attempts.' in output

    def test_human_output_reports_live_worker_with_metrics(
        self, dbos_environment: Any, monkeypatch: pytest.MonkeyPatch, mocker
    ) -> None:
        fake_psutil = types.ModuleType('psutil')
        fake_psutil.Process = lambda: types.SimpleNamespace(
            cpu_percent=lambda interval=None: 42.0,
            memory_info=lambda: types.SimpleNamespace(rss=1024 * 1024 * 1024),
        )
        monkeypatch.setitem(sys.modules, 'psutil', fake_psutil)
        write_heartbeat('live-worker', heartbeat_id=uuid.uuid4(), queues=('default',), has_metrics=True)
        output = status()
        assert (
            f'live-worker: {socket.gethostname()} pid {os.getpid()}, queues default, CPU 42.0%, RSS 1024.0 MB' in output
        )

    def test_human_output_reports_live_worker_without_metrics(self, dbos_environment: Any) -> None:
        write_heartbeat('plain-worker', heartbeat_id=uuid.uuid4(), has_metrics=False)
        output = status()
        assert (
            f'plain-worker: {socket.gethostname()} pid {os.getpid()}, install django-dbos[metrics] for CPU/memory'
            in output
        )

    def test_human_output_flags_executor_without_heartbeat(self, dbos_environment: Any, mocker) -> None:
        # An executor with active workflows but no heartbeat row at all.
        ghost = types.SimpleNamespace(
            status='PENDING', queue_name='default', executor_id='ghost-worker', created_at=1, dequeued_at=2
        )
        mocker.patch('django_dbos.capacity._active_workflows', return_value=[ghost])
        output = status()
        assert 'ghost-worker: NO HEARTBEAT (worker down, or django_dbos migrations not applied)' in output

    def test_human_output_flags_stale_heartbeat(self, dbos_environment: Any) -> None:
        write_heartbeat('dead-worker', heartbeat_id=uuid.uuid4())
        WorkerHeartbeat.objects.update(last_seen_at=timezone.now() - timedelta(seconds=STALE_AFTER_SECONDS + 1))
        output = status()
        assert f'dead-worker: {socket.gethostname()} pid {os.getpid()}, STALE (last seen' in output

    def test_human_output_with_no_worker_at_all(self, dbos_environment: Any, mocker) -> None:
        # Nothing active, no heartbeat: the section is empty.
        mocker.patch('django_dbos.capacity._active_workflows', return_value=[])
        output = status()
        assert 'Workers:' in output
        assert '(none)' in output

    def test_unreachable_system_database_is_a_command_error(self, dbos_environment: Any, mocker) -> None:
        error = OperationalError('SELECT 1', {}, Exception('postgresql://user:hunter2@db/dbos: connection refused'))
        mocker.patch('django_dbos.capacity.get_client', side_effect=error)
        err = StringIO()
        with contextlib.redirect_stdout(StringIO()), contextlib.redirect_stderr(err), pytest.raises(Exit) as excinfo:
            call_command('dbos', 'status')
        assert excinfo.value.exit_code == 1
        assert 'OperationalError' in err.getvalue()
        assert 'hunter2' not in err.getvalue()
        assert 'user:***@db' in err.getvalue()

    def test_human_output_with_queue_filter(self, dbos_environment: Any) -> None:
        output = status('--queues', 'emails')
        assert 'Queue "emails"' in output
        assert 'Queue "default"' not in output

    def test_json_output_with_queue_filter(self, dbos_environment: Any) -> None:
        output = status('--json', '--queues', 'emails')
        payload = json.loads(output)
        assert set(payload['queues']) == {'emails'}
