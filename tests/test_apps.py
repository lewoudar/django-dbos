"""Tests for the Django app config: ready() gating and runserver detection."""

import pytest
from django.apps import apps

from django_dbos.apps import _under_runserver_reloader_parent


class TestRunserverReloaderDetection:
    @pytest.mark.parametrize(
        ('argv', 'run_main', 'expected'),
        [
            pytest.param(['manage.py', 'runserver'], None, True, id='runserver-parent'),
            pytest.param(['manage.py', 'runserver'], 'true', False, id='runserver-child'),
            pytest.param(['manage.py', 'dbos_worker'], None, False, id='other-command'),
        ],
    )
    def test_detection(self, monkeypatch, argv, run_main, expected):
        monkeypatch.setattr('sys.argv', argv)
        if run_main is None:
            monkeypatch.delenv('RUN_MAIN', raising=False)
        else:
            monkeypatch.setenv('RUN_MAIN', run_main)
        assert _under_runserver_reloader_parent() is expected


class TestReady:
    def test_ready_without_auto_init_skips_initialization(self, settings, mocker):
        ensure_initialized = mocker.patch('django_dbos.dbos_setup.ensure_initialized')
        settings.DJANGO_DBOS = {'AUTO_INIT': False, 'ADMIN_ENABLED': False}
        apps.get_app_config('django_dbos').ready()
        ensure_initialized.assert_not_called()

    def test_ready_with_auto_init_initializes(self, settings, mocker):
        ensure_initialized = mocker.patch('django_dbos.dbos_setup.ensure_initialized')
        settings.DJANGO_DBOS = {'AUTO_INIT': True, 'ADMIN_ENABLED': False}
        apps.get_app_config('django_dbos').ready()
        ensure_initialized.assert_called_once_with()
