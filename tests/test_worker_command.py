"""Tests for the dbos worker management command (mocked DBOS launch)."""

import signal

import pytest
from click.exceptions import BadParameter
from django.core.management import call_command


@pytest.fixture()
def worker_mocks(mocker):
    """Patch everything the worker command touches at runtime."""
    return {
        'autodiscover': mocker.patch('django_dbos.discovery.autodiscover_tasks'),
        'ensure_launched': mocker.patch('django_dbos.dbos_setup.ensure_launched'),
        'register_cron_schedules': mocker.patch('django_dbos.scheduling.register_cron_schedules'),
        'shutdown': mocker.patch('django_dbos.dbos_setup.shutdown'),
        'heartbeat_cls': mocker.patch('django_dbos.management.commands.dbos.HeartbeatThread'),
        'find_spec': mocker.patch('importlib.util.find_spec', return_value=None),
        'event_wait': mocker.patch('threading.Event.wait', return_value=True),
        # Never install real handlers in the pytest process: they would outlive the test.
        'signal': mocker.patch('signal.signal'),
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
        executor_id='local', queues=('default', 'emails', 'per_user'), has_metrics=False
    )
    heartbeat = worker_mocks['heartbeat_cls'].return_value
    heartbeat.start.assert_called_once_with()
    heartbeat.stop.assert_called_once_with()

    worker_mocks['shutdown'].assert_called_once_with(workflow_completion_timeout_sec=10)
    assert 'DBOS worker started' in output
    assert 'Shutting down' in output


@pytest.mark.parametrize('timeout', [0, 25])
def test_worker_shutdown_timeout_is_forwarded(dbos_environment, worker_mocks, timeout):
    run_worker('--shutdown-timeout', str(timeout))

    worker_mocks['shutdown'].assert_called_once_with(workflow_completion_timeout_sec=timeout)


def test_worker_rejects_a_negative_shutdown_timeout(dbos_environment, worker_mocks):
    with pytest.raises(BadParameter, match=r'-1 is not in the range x>=0'):
        run_worker('--shutdown-timeout', '-1')

    worker_mocks['ensure_launched'].assert_not_called()


def test_worker_stops_on_sigterm_or_sigint(dbos_environment, worker_mocks, mocker):
    handlers = {}
    worker_mocks['signal'].side_effect = handlers.__setitem__
    stop_event_set = mocker.patch('threading.Event.set')

    def wait(timeout=None):
        handlers[signal.SIGTERM](signal.SIGTERM, None)  # simulate `kill <pid>`
        return True

    worker_mocks['event_wait'].side_effect = wait

    run_worker()

    assert set(handlers) == {signal.SIGTERM, signal.SIGINT}
    stop_event_set.assert_called_once_with()
    worker_mocks['heartbeat_cls'].return_value.stop.assert_called_once_with()
    worker_mocks['shutdown'].assert_called_once_with(workflow_completion_timeout_sec=10)
