"""Report on DBOS worker capacity: queue backlogs, waiting times and load."""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand

from django_dbos import capacity, discovery

_ADVICE_STYLES = {"SCALE_UP": "ERROR", "SCALE_DOWN": "WARNING", "OK": "SUCCESS"}


class Command(BaseCommand):
    help = (
        "Report on DBOS worker capacity (queue backlogs, waiting times, per-executor "
        "load) with a per-queue advice: SCALE_UP / SCALE_DOWN / OK."
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output machine-readable JSON (for alerting or scripting).",
        )
        parser.add_argument(
            "--queues",
            default="",
            help="Comma-separated queue names to report on (default: all).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        discovery.autodiscover_tasks()

        queue_filter = [name.strip() for name in options["queues"].split(",") if name.strip()] or None
        report = capacity.collect(queues=queue_filter)

        if options["json"]:
            self.stdout.write(json.dumps(report, indent=2, default=str))
            return

        self._print_human(report)

    def _print_human(self, report: dict[str, Any]) -> None:
        for queue_name, metrics in report["queues"].items():
            advice = metrics["advice"]
            style = getattr(self.style, _ADVICE_STYLES.get(advice, "SUCCESS"), self.style.SUCCESS)
            self.stdout.write(f'\nQueue "{queue_name}"  [{style(advice)}]')
            self.stdout.write(
                f"  Backlog: {metrics['enqueued']} (delayed: {metrics['delayed']})   Running: {metrics['running']}"
            )
            if metrics["backlog_age_seconds"] is not None:
                self.stdout.write(f"  Oldest waiting workflow: {metrics['backlog_age_seconds']:.0f}s")
            if metrics["max_queue_wait_ms"] is not None:
                self.stdout.write(f"  Max queue wait (running): {metrics['max_queue_wait_ms']} ms")
            self.stdout.write(
                f"  Declared concurrency: worker={metrics['worker_concurrency']} global={metrics['global_concurrency']}"
            )

        self._print_workers(report)

        self.stdout.write("")
        exceeded = report["max_recovery_attempts_exceeded"]
        if exceeded:
            self.stdout.write(self.style.ERROR(f"{exceeded} workflow(s) exhausted their recovery attempts."))
        else:
            self.stdout.write("No workflow exhausted its recovery attempts.")

    def _print_workers(self, report: dict[str, Any]) -> None:
        heartbeats = report.get("worker_heartbeats") or {}
        executor_ids = set(report.get("executors") or {}) | set(heartbeats)
        self.stdout.write("\nWorkers:")
        if not executor_ids:
            self.stdout.write("  (none)")
            return
        for executor_id in sorted(executor_ids):
            heartbeat = heartbeats.get(executor_id)
            if heartbeat is None:
                self.stdout.write(
                    f"  {executor_id}: NO HEARTBEAT (worker down, no shared cache, or psutil extra missing)"
                )
                continue
            parts = [f"pid {heartbeat.get('pid')}"]
            if heartbeat.get("has_metrics"):
                parts.append(f"CPU {heartbeat.get('cpu_percent')}%")
                parts.append(f"RSS {heartbeat.get('memory_rss_mb')} MB")
            else:
                parts.append("install django-dbos[metrics] for CPU/memory")
            self.stdout.write(f"  {executor_id}: {', '.join(parts)}")
