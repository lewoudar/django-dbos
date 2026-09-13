"""Tests for the capacity heuristics and the dbos_status command."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from django_dbos import capacity
from django_dbos.capacity import advise_queue


def _metrics(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "enqueued": 0,
        "delayed": 0,
        "running": 0,
        "backlog_age_seconds": None,
        "max_queue_wait_ms": None,
        "worker_concurrency": None,
        "global_concurrency": None,
        "advice": "OK",
    }
    base.update(overrides)
    return base


class TestAdviseQueue:
    def test_idle_queue_suggests_scale_down(self):
        assert advise_queue(_metrics()) == "SCALE_DOWN"

    def test_low_activity_is_ok(self):
        metrics = _metrics(enqueued=1, backlog_age_seconds=5.0, running=1, worker_concurrency=4)
        assert advise_queue(metrics) == "OK"

    def test_old_backlog_suggests_scale_up(self):
        metrics = _metrics(enqueued=10, backlog_age_seconds=120.0, running=2, worker_concurrency=4)
        assert advise_queue(metrics) == "SCALE_UP"

    def test_fresh_backlog_is_ok(self):
        metrics = _metrics(enqueued=10, backlog_age_seconds=3.0, running=2, worker_concurrency=4)
        assert advise_queue(metrics) == "OK"

    def test_running_activity_prevents_scale_down(self):
        metrics = _metrics(running=3, worker_concurrency=4)
        assert advise_queue(metrics) == "OK"


# --- Integration (real system database) ---


@pytest.mark.integration
class TestCollect:
    def test_collect_reports_delayed_workflows(self, dbos_environment):
        from .tasks import add

        deferred = add.using(run_after=timezone.now() + timedelta(hours=1))
        deferred.enqueue(1, 2)

        report = capacity.collect(queues=["default"])
        assert report["queues"]["default"]["delayed"] == 1
        assert report["queues"]["default"]["advice"] in ("OK", "SCALE_UP", "SCALE_DOWN")
        assert report["max_recovery_attempts_exceeded"] == 0

        # JSON output stays parseable
        payload = json.loads(json.dumps(report, default=str))
        assert "queues" in payload and "executors" in payload

    def test_collect_filters_queues(self, dbos_environment):
        report = capacity.collect(queues=["emails"])
        assert list(report["queues"]) == ["emails"]

    def test_worker_heartbeat_visible_in_report(self, dbos_environment):
        from dbos import DBOS

        from django_dbos.heartbeat import write_heartbeat

        write_heartbeat(DBOS.executor_id or "local", has_metrics=False)
        report = capacity.collect()
        assert (DBOS.executor_id or "local") in report["worker_heartbeats"]
        assert report["worker_heartbeats"][DBOS.executor_id or "local"]["has_metrics"] is False

    def test_command_json_output(self, dbos_environment: Any, capsys: Any) -> None:
        from django.core.management import call_command

        call_command("dbos_status", "--json")
        output = capsys.readouterr().out
        # DBOS/structlog may log to stdout: parse from the first JSON object.
        payload = json.loads(output[output.index("{") :])
        assert set(payload) == {
            "queues",
            "executors",
            "worker_heartbeats",
            "max_recovery_attempts_exceeded",
        }
        assert set(payload["queues"]) == {"default", "emails"}

    def test_command_human_output(self, dbos_environment: Any, capsys: Any) -> None:
        from django.core.management import call_command

        call_command("dbos_status")
        output = capsys.readouterr().out
        assert 'Queue "default"' in output
        assert "Workers:" in output
