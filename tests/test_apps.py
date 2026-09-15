"""Tests for the Django app config: ready() registers the admin pages and nothing DBOS-related."""

from django.apps import apps


class TestReady:
    def test_ready_registers_admin_and_leaves_dbos_alone(self, settings, mocker):
        register_admin_sites = mocker.patch('django_dbos.admin.register_admin_sites')
        ensure_initialized = mocker.patch('django_dbos.dbos_setup.ensure_initialized')
        ensure_launched = mocker.patch('django_dbos.dbos_setup.ensure_launched')
        get_client = mocker.patch('django_dbos.client.get_client')

        apps.get_app_config('django_dbos').ready()

        register_admin_sites.assert_called_once_with()
        ensure_initialized.assert_not_called()
        ensure_launched.assert_not_called()
        get_client.assert_not_called()

    def test_system_checks_are_registered(self):
        from django.core.checks import registry

        from django_dbos.checks import check_dbos_settings, check_system_database_engine

        registered = set(registry.registry.registered_checks)
        assert check_dbos_settings in registered
        assert check_system_database_engine in registered
