"""`manage.py dbos <subcommand>`: migrate the system database, run the worker, report capacity.

Built with django-click (``djclick``), grouping what used to be three separate
flat commands under one namespace, mirroring the DBOS SDK's own CLI. The
`main` group below is a plain, importable module-level object: a future
companion UI package can extend it with its own subcommands via
``from django_dbos.management.commands.dbos import main as dbos_group`` and
``dbos_group.add_command(...)`` in its own ``AppConfig.ready()``.
"""

from __future__ import annotations

import importlib.util
import json
import signal
import threading
from typing import Any

import djclick as click
from django.core.management.base import CommandError

from django_dbos import capacity, dbos_setup, discovery, system_db
from django_dbos import settings as settings_module
from django_dbos.exceptions import ImproperlyConfiguredDbos, describe_error, system_database_errors


@click.group()
def main() -> None:
    """DBOS operations: migrate the system database, run the worker, report capacity."""


@main.command()
@click.option(
    '--app-role',
    default=None,
    help='Database role to grant the DBOS schema permissions to (the role your workers and web processes use).',
)
def migrate(app_role: str | None) -> None:
    """Create the DBOS system database and apply its migrations, without launching a worker."""
    system_db.migrate_system_database(echo=click.echo, app_role=app_role)
    click.secho('DBOS system database is up to date.', fg='green')


@main.command()
def worker() -> None:
    """Run the DBOS worker: execute Django tasks durably and process cron schedules."""
    from django_dbos import scheduling

    discovery.autodiscover_tasks()
    dbos_setup.ensure_launched()
    scheduling.register_cron_schedules()

    stop_event = threading.Event()

    def _stop(signum: int, frame: Any) -> None:
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _stop)

    from dbos import DBOS

    from django_dbos.heartbeat import HeartbeatThread

    heartbeat_thread = HeartbeatThread(
        executor_id=DBOS.executor_id or 'local',
        queues=tuple(queue.name for queue in settings_module.resolve_queues()),
        has_metrics=importlib.util.find_spec('psutil') is not None,
    )
    heartbeat_thread.start()

    click.echo('DBOS worker started. Press Ctrl+C to stop.')
    try:
        stop_event.wait()
    finally:
        click.echo('Shutting down DBOS worker...')
        heartbeat_thread.stop()
        dbos_setup.shutdown()


#: Capacity advice -> click color for the status badge in the human-readable report.
_ADVICE_COLORS = {'SCALE_UP': 'red', 'SCALE_DOWN': 'yellow', 'OK': 'green'}


@main.command(name='status')
@click.option('--json', 'as_json', is_flag=True, help='Output machine-readable JSON (for alerting or scripting).')
@click.option('--queues', default='', help='Comma-separated queue names to report on (default: all).')
def status_command(as_json: bool, queues: str) -> None:
    """Report on DBOS worker capacity: queue backlogs, waiting times and load."""
    queue_filter = [name.strip() for name in queues.split(',') if name.strip()] or None
    try:
        report = capacity.collect(queues=queue_filter)
    except (ImproperlyConfiguredDbos, *system_database_errors()) as exc:
        raise CommandError(f'DBOS system database unavailable: {describe_error(exc)}') from None

    if as_json:
        click.echo(json.dumps(report, indent=2, default=str))
        return

    _print_human(report)


def _print_human(report: dict[str, Any]) -> None:
    for queue_name, metrics in report['queues'].items():
        advice = metrics['advice']
        color = _ADVICE_COLORS.get(advice, 'green')
        click.echo(f'\nQueue "{queue_name}"  [{click.style(advice, fg=color)}]')
        click.echo(f'  Backlog: {metrics["enqueued"]} (delayed: {metrics["delayed"]})   Running: {metrics["running"]}')
        if metrics['backlog_age_seconds'] is not None:
            click.echo(f'  Oldest waiting workflow: {metrics["backlog_age_seconds"]:.0f}s')
        if metrics['max_queue_wait_ms'] is not None:
            click.echo(f'  Max queue wait (running): {metrics["max_queue_wait_ms"]} ms')
        click.echo(
            f'  Declared concurrency: worker={metrics["worker_concurrency"]} global={metrics["global_concurrency"]}'
        )

    _print_workers(report)

    click.echo('')
    exceeded = report['max_recovery_attempts_exceeded']
    if exceeded:
        click.secho(f'{exceeded} workflow(s) exhausted their recovery attempts.', fg='red')
    else:
        click.echo('No workflow exhausted its recovery attempts.')


def _print_workers(report: dict[str, Any]) -> None:
    workers_by_executor: dict[str, list[dict[str, Any]]] = {}
    for worker_row in report.get('workers') or []:
        workers_by_executor.setdefault(worker_row['executor_id'], []).append(worker_row)
    executor_ids = set(report.get('executors') or {}) | set(workers_by_executor)
    click.echo('\nWorkers:')
    if not executor_ids:
        click.echo('  (none)')
        return
    for executor_id in sorted(executor_ids):
        workers = workers_by_executor.get(executor_id)
        if not workers:
            click.echo(f'  {executor_id}: NO HEARTBEAT (worker down, or django_dbos migrations not applied)')
            continue
        for worker_row in sorted(workers, key=lambda item: (item['hostname'], item['pid'])):
            parts = [f'{worker_row["hostname"]} pid {worker_row["pid"]}']
            if not worker_row.get('alive', True):
                click.echo(
                    f'  {executor_id}: {parts[0]}, {click.style("STALE", fg="red")} (last seen {worker_row["last_seen_at"]})'
                )
                continue
            if worker_row.get('queues'):
                parts.append(f'queues {",".join(worker_row["queues"])}')
            if worker_row.get('has_metrics'):
                parts.append(f'CPU {worker_row.get("cpu_percent")}%')
                parts.append(f'RSS {worker_row.get("memory_rss_mb")} MB')
            else:
                parts.append('install django-dbos[metrics] for CPU/memory')
            click.echo(f'  {executor_id}: {", ".join(parts)}')
