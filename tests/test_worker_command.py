"""Tests for the dbos worker management command (mocked DBOS launch)."""

import pytest
from django.core.management import call_command


@pytest.fixture()
def worker_mocks(mocker):
    """Patch everything the worker command touches at runtime."""
    return {
        'autodiscover': mocker.patch('django_dbos.discovery.autodiscover_tasks'),
        'ensure_launched': mocker.patch('django_dbos.dbos_setup.ensure_launched'),
        'register_cron_schedules': mocker.patch('django_dbos.scheduling.register_cron_schedules'),
        'shutdown': mocker.patch('django_dbos.dbos_setup.shutdown'),
        'heartbeat_cls': mocker.patch('django_dbos.heartbeat.HeartbeatThread'),
        'find_spec': mocker.patch('importlib.util.find_spec', return_value=None),
        'event_wait': mocker.patch('threading.Event.wait', return_value=True),
    }


def run_worker(*args: str) -> str:
    import contextlib
    from io import StringIO

    out = StringIO()
    with contextlib.redirect_stdout(out):
        call_command('dbos', 'worker', *args)
    return out.getvalue()


def test_worker_default_run(dbos_environment, worker_mocks):
    output = run_worker()

    worker_mocks['autodiscover'].assert_called_once_with()
    worker_mocks['ensure_launched'].assert_called_once_with()
    worker_mocks['register_cron_schedules'].assert_called_once_with()

    worker_mocks['heartbeat_cls'].assert_called_once_with(
        executor_id='local', queues=('default', 'emails'), has_metrics=False
    )
    heartbeat = worker_mocks['heartbeat_cls'].return_value
    heartbeat.start.assert_called_once_with()
    heartbeat.stop.assert_called_once_with()

    worker_mocks['shutdown'].assert_called_once_with()
    assert 'DBOS worker started' in output
    assert 'Shutting down' in output


def test_worker_no_scheduler_flag_skips_cron_registration(dbos_environment, worker_mocks):
    run_worker('--no-scheduler')
    worker_mocks['register_cron_schedules'].assert_not_called()
