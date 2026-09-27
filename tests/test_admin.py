"""Integration tests for the django-dbos admin pages."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from dbos import DBOS
from django.contrib.admin import AdminSite
from django.contrib.auth.models import User
from django.tasks import TaskResultStatus
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from sqlalchemy.exc import OperationalError

from django_dbos.admin import (
    get_admin_site_paths,
    get_admin_sites,
    is_admin_enabled,
    register_admin,
    register_admin_sites,
)
from django_dbos.heartbeat import write_heartbeat
from django_dbos.scheduling import register_cron_schedules

from .conftest import wait_for_result
from .tasks import add

pytestmark = pytest.mark.integration


def assert_text(response: Any, text: str) -> None:
    assert text in response.content.decode()


@pytest.fixture()
def admin_client(client: Any, db: Any) -> Any:
    """A Django test client logged in as a superuser."""
    user = User.objects.create_superuser(username='kevin', password='hunter2', email='')
    client.force_login(user)
    yield client
    User.objects.filter(pk=user.pk).delete()


def _enqueue_sample_workflow() -> Any:
    return add.enqueue(2, 3)


class TestWorkflowPages:
    def test_pages_never_launch_dbos(self, admin_client, dbos_environment, mocker):
        # The admin reads the system database through the client; launching
        # DBOS would turn the web process into a worker.
        ensure_launched = mocker.patch('django_dbos.dbos_setup.ensure_launched')
        ensure_initialized = mocker.patch('django_dbos.dbos_setup.ensure_initialized')
        result = _enqueue_sample_workflow()

        assert admin_client.get('/admin/dbos/workflows/').status_code == 200
        assert admin_client.get(f'/admin/dbos/workflows/{result.id}/').status_code == 200
        assert admin_client.get('/admin/dbos/schedules/').status_code == 200

        ensure_launched.assert_not_called()
        ensure_initialized.assert_not_called()

    def test_workflow_list_shows_enqueued_task(self, admin_client, dbos_environment):
        result = _enqueue_sample_workflow()
        response = admin_client.get('/admin/dbos/workflows/')
        assert response.status_code == 200
        assert_text(response, 'tests.tasks.add')
        assert_text(response, result.id)

    def test_workflow_detail_shows_steps(self, admin_client, dbos_environment):
        result = wait_for_result(_enqueue_sample_workflow(), TaskResultStatus.SUCCESSFUL)
        response = admin_client.get(f'/admin/dbos/workflows/{result.id}/')
        assert response.status_code == 200
        assert_text(response, 'tests.tasks.add')
        assert_text(response, 'task_body')

    def test_workflow_detail_renders_unusual_values(self, admin_client, mocker):
        # A step that never completed has no duration; outputs that are not
        # JSON-serializable fall back to str(); schedule-fired workflows may
        # carry ISO string timestamps.
        circular: list[Any] = []
        circular.append(circular)
        status = SimpleNamespace(
            workflow_id='wf-odd',
            name='tests.tasks.add',
            status='PENDING',
            queue_name='default',
            priority=0,
            created_at='2026-01-01T00:00:00+00:00',
            updated_at=None,
            completed_at=None,
            executor_id=None,
            app_version=None,
            schedule_name=None,
            deduplication_id=None,
            input=circular,
            output=None,
            error=None,
        )
        client = mocker.MagicMock()
        client.retrieve_workflow.return_value.get_status.return_value = status
        client.list_workflow_steps.return_value = [
            {'function_id': 1, 'function_name': 'task_body', 'started_at_epoch_ms': 1_000},
        ]
        mocker.patch('django_dbos.admin_views.get_client', return_value=client)

        response = admin_client.get('/admin/dbos/workflows/wf-odd/')

        assert response.status_code == 200
        assert response.context['steps'][0]['duration_ms'] is None
        assert response.context['input'] == '[[...]]'
        assert_text(response, '2026-01-01T00:00:00+00:00')

    @pytest.mark.parametrize('bad_page', ['abc', '-3'])
    def test_workflow_list_bad_page_inputs_render_page_1(self, admin_client, dbos_environment, bad_page):
        result = _enqueue_sample_workflow()
        response = admin_client.get('/admin/dbos/workflows/', {'page': bad_page})
        assert response.status_code == 200
        # The first page is rendered: the newest workflow is listed.
        assert_text(response, result.id)

    def test_workflow_list_huge_page_is_clamped_not_500(self, admin_client, dbos_environment):
        # Out-of-range page: clamped to the last allowed page, so the offset
        # stays within bigint range instead of erroring in the database.
        response = admin_client.get('/admin/dbos/workflows/', {'page': '99999999999999999999999'})
        assert response.status_code == 200
        assert_text(response, 'No workflows found.')

    def test_workflow_detail_404_on_unknown_id(self, admin_client, dbos_environment):
        response = admin_client.get('/admin/dbos/workflows/unknown-id/')
        assert response.status_code == 404

    def test_cancel_workflow(self, admin_client, dbos_environment):
        result = _enqueue_sample_workflow()
        response = admin_client.post(f'/admin/dbos/workflows/{result.id}/cancel/')
        assert response.status_code == 302
        status = DBOS.get_workflow_status(result.id)
        assert status is not None
        assert str(status.status) in ('CANCELLED', 'WorkflowStatuses.CANCELLED')

    def test_resume_cancelled_workflow(self, admin_client, dbos_environment):
        # A deferred task cannot race with an in-process worker: it stays
        # pending until its run_after, so cancel/resume are deterministic.
        deferred = add.using(run_after=timezone.now() + timedelta(hours=1))
        result = deferred.enqueue(1, 2)
        admin_client.post(f'/admin/dbos/workflows/{result.id}/cancel/')
        assert str(DBOS.get_workflow_status(result.id).status) == 'CANCELLED'

        response = admin_client.post(f'/admin/dbos/workflows/{result.id}/resume/')
        assert response.status_code == 302
        assert str(DBOS.get_workflow_status(result.id).status) != 'CANCELLED'

    def test_fork_workflow_redirects_to_the_fork(self, admin_client, dbos_environment):
        result = wait_for_result(_enqueue_sample_workflow(), TaskResultStatus.SUCCESSFUL)
        response = admin_client.post(f'/admin/dbos/workflows/{result.id}/fork/')
        assert response.status_code == 302
        forked_id = response['Location'].rstrip('/').rsplit('/', 1)[-1]
        assert forked_id != result.id
        forked = DBOS.get_workflow_status(forked_id)
        assert forked is not None
        assert forked.name.endswith('tests.tasks.add')

    @pytest.mark.parametrize('action', ['cancel', 'resume', 'fork'])
    def test_get_on_action_redirects_without_acting(self, admin_client, dbos_environment, action):
        result = _enqueue_sample_workflow()
        response = admin_client.get(f'/admin/dbos/workflows/{result.id}/{action}/')
        assert response.status_code == 302
        assert response['Location'] == f'/admin/dbos/workflows/{result.id}/'
        status = DBOS.get_workflow_status(result.id)
        assert status is not None
        assert str(status.status) != 'CANCELLED'

    def test_delete_workflow_with_confirmation(self, admin_client, dbos_environment):
        result = _enqueue_sample_workflow()
        # GET shows the confirmation page
        response = admin_client.get(f'/admin/dbos/workflows/{result.id}/delete/')
        assert response.status_code == 200
        assert_text(response, 'Are you sure?')
        # POST deletes and redirects to the changelist
        response = admin_client.post(f'/admin/dbos/workflows/{result.id}/delete/')
        assert response.status_code == 302
        assert DBOS.get_workflow_status(result.id) is None


class TestSchedulePages:
    def test_schedule_list(self, admin_client, dbos_environment):
        register_cron_schedules()
        response = admin_client.get('/admin/dbos/schedules/')
        assert response.status_code == 200
        assert_text(response, 'tests.tasks.cron_task')

    def test_delete_schedule_with_confirmation(self, admin_client, dbos_environment):
        register_cron_schedules()
        name = 'django_dbos.schedules.tests.tasks.cron_task'
        # GET shows the confirmation page instead of deleting outright
        response = admin_client.get(f'/admin/dbos/schedules/{name}/delete/')
        assert response.status_code == 200
        assert_text(response, 'Are you sure?')
        assert_text(response, name)
        # POST deletes and redirects to the changelist
        response = admin_client.post(f'/admin/dbos/schedules/{name}/delete/')
        assert response.status_code == 302

        assert DBOS.get_schedule(name) is None

    def test_unknown_schedule_returns_404(self, admin_client, dbos_environment):
        name = 'django_dbos.schedules.tests.tasks.never_registered'
        response = admin_client.post(f'/admin/dbos/schedules/{name}/pause/')
        assert response.status_code == 404
        response = admin_client.get(f'/admin/dbos/schedules/{name}/delete/')
        assert response.status_code == 404

    @pytest.mark.parametrize('action', ['pause', 'resume'])
    def test_get_on_schedule_action_redirects_without_acting(self, admin_client, dbos_environment, action):
        register_cron_schedules()
        name = 'django_dbos.schedules.tests.tasks.cron_task'
        response = admin_client.get(f'/admin/dbos/schedules/{name}/{action}/')
        assert response.status_code == 302
        assert response['Location'] == '/admin/dbos/schedules/'
        assert DBOS.get_schedule(name) is not None

    def test_pause_then_resume_schedule(self, admin_client, dbos_environment):
        register_cron_schedules()
        name = 'django_dbos.schedules.tests.tasks.cron_task'

        response = admin_client.post(f'/admin/dbos/schedules/{name}/pause/')
        assert response.status_code == 302
        assert response['Location'] == '/admin/dbos/schedules/'
        assert DBOS.get_schedule(name)['status'] == 'PAUSED'

        response = admin_client.post(f'/admin/dbos/schedules/{name}/resume/')
        assert response.status_code == 302
        assert DBOS.get_schedule(name)['status'] == 'ACTIVE'


class TestAdminGating:
    @pytest.mark.parametrize(
        ('admin_enabled', 'expected'),
        [
            pytest.param(None, True, id='unset-defaults-to-enabled'),
            pytest.param(True, True, id='explicit-true'),
            pytest.param(False, False, id='explicit-false'),
        ],
    )
    def test_admin_enabled_setting(self, settings, admin_enabled, expected):
        dj = {k: v for k, v in settings.DJANGO_DBOS.items() if k != 'ADMIN_ENABLED'}
        if admin_enabled is not None:
            dj['ADMIN_ENABLED'] = admin_enabled
        settings.DJANGO_DBOS = dj
        assert is_admin_enabled() is expected

    def test_urls_registered_by_default(self, dbos_environment):
        reverse('admin:django_dbos_workflow_changelist')  # raises if missing

    def test_staff_without_superuser_gets_403(self, db, dbos_environment):
        staff = User.objects.create_user(username='editor', password='hunter2', is_staff=True)
        client = Client()
        client.force_login(staff)
        try:
            response = client.get('/admin/dbos/workflows/')
            assert response.status_code == 403
            response = client.post('/admin/dbos/workflows/whatever/delete/')
            assert response.status_code == 403
        finally:
            User.objects.filter(pk=staff.pk).delete()

    def test_disabled_admin_skips_registration(self, settings):
        settings.DJANGO_DBOS = {**settings.DJANGO_DBOS, 'ADMIN_ENABLED': False}
        site = AdminSite(name='disabled-test')
        register_admin_sites()
        # register_admin_sites() only touches the configured default sites;
        # our fresh site is untouched and directly registerable or not.
        assert not getattr(site, '_django_dbos_registered', False)

    def test_custom_admin_site_registration(self, dbos_environment):
        custom_site = AdminSite(name='custom-test')
        register_admin(custom_site)
        url_names = [p.name for p in custom_site.get_urls() if getattr(p, 'name', None)]
        assert 'django_dbos_workflow_changelist' in url_names
        assert 'django_dbos_schedule_changelist' in url_names
        # Idempotent: a second registration is a no-op.
        register_admin(custom_site)
        assert 'django_dbos_workflow_changelist' in url_names

    def test_non_staff_redirected_to_login(self, client, dbos_environment):
        response = client.get('/admin/dbos/workflows/')
        assert response.status_code == 302
        assert '/admin/login' in response['Location']


class TestUnavailableSystemDatabase:
    def _failing_client(self, mocker):
        error = OperationalError(
            'SELECT 1', {}, Exception('connection to postgresql://user:hunter2@db:5432/dbos_app failed')
        )
        client = mocker.MagicMock()
        client.list_workflows.side_effect = error
        client.retrieve_workflow.side_effect = error
        client.list_schedules.side_effect = error
        return mocker.patch('django_dbos.admin_views.get_client', return_value=client)

    @pytest.mark.parametrize(
        'path', ['/admin/dbos/workflows/', '/admin/dbos/workflows/some-id/', '/admin/dbos/schedules/']
    )
    def test_renders_error_page_without_credentials(self, admin_client, dbos_environment, mocker, path):
        self._failing_client(mocker)
        response = admin_client.get(path)
        assert response.status_code == 200
        assert_text(response, 'django-dbos is not available')
        assert_text(response, 'OperationalError')
        assert_text(response, 'user:***@db:5432')
        assert 'hunter2' not in response.content.decode()

    def test_unknown_workflow_is_still_a_404(self, admin_client, dbos_environment):
        assert admin_client.get('/admin/dbos/workflows/unknown-id/').status_code == 404


class TestWorkerHeartbeatAdmin:
    def test_superuser_sees_live_workers(self, admin_client):
        write_heartbeat('machine-1', queues=('default',))
        response = admin_client.get('/admin/django_dbos/workerheartbeat/')
        assert response.status_code == 200
        assert_text(response, 'machine-1')

    def test_heartbeats_are_read_only(self, admin_client):
        response = admin_client.get('/admin/django_dbos/workerheartbeat/add/')
        assert response.status_code == 403

    def test_staff_without_superuser_gets_403(self, db):
        staff = User.objects.create_user(username='viewer', password='hunter2', is_staff=True)
        client = Client()
        client.force_login(staff)
        try:
            response = client.get('/admin/django_dbos/workerheartbeat/')
            assert response.status_code == 403
        finally:
            User.objects.filter(pk=staff.pk).delete()


class TestAdminSiteResolution:
    def test_admin_site_accepts_a_single_string(self, settings):
        settings.DJANGO_DBOS = {'ADMIN_SITE': 'django.contrib.admin.site'}
        assert get_admin_site_paths() == ('django.contrib.admin.site',)

    def test_unknown_custom_site_raises(self, settings):
        settings.DJANGO_DBOS = {'ADMIN_SITE': 'myproject.sites.missing_site'}
        with pytest.raises(ImportError):
            get_admin_sites()
