"""Run the DBOS worker for Django tasks."""

from __future__ import annotations

import signal
import threading
from typing import Any

from django.core.management.base import BaseCommand

from django_dbos import dbos_setup, discovery


class Command(BaseCommand):
    help = "Run the DBOS worker: execute Django tasks durably and process cron schedules."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--no-scheduler",
            action="store_true",
            help="Do not process cron schedules in this process.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        discovery.autodiscover_tasks()
        dbos_setup.ensure_launched()

        if not options["no_scheduler"]:
            from django_dbos import scheduling

            scheduling.register_cron_schedules()

        stop_event = threading.Event()

        def _stop(signum: int, frame: Any) -> None:
            stop_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, _stop)

        self.stdout.write("DBOS worker started. Press Ctrl+C to stop.")
        try:
            stop_event.wait()
        finally:
            self.stdout.write("Shutting down DBOS worker...")
            dbos_setup.shutdown()
