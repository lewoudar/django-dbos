"""Integration tests for the django-dbos admin pages."""

from __future__ import annotations

import uuid
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
    WorkerHeartbeatAdmin,
    get_admin_site_paths,
    get_admin_sites,
    is_admin_enabled,
    register_admin,
    register_admin_sites,
)
from django_dbos.admin_views import STEP_RESULT_INLINE_LENGTH
from django_dbos.heartbeat import write_heartbeat
from django_dbos.models import WorkerHeartbeat
from django_dbos.scheduling import register_cron_schedules
from tests.helpers import set_dbos_options

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


def _identity(response: Any) -> dict[str, Any]:
    """The detail page's workflow fields, label -> value."""
    return {field['label']: field['value'] for field in response.context['identity']}


def _workflow_status(**overrides: Any) -> SimpleNamespace:
    """A workflow status as returned by DBOSClient, every field empty unless overridden."""
    values: dict[str, Any] = {
        'workflow_id': 'wf-1',
        'name': 'django_dbos.tasks.tests.tasks.add',
        'status': 'SUCCESS',
        'queue_name': 'default',
        'queue_partition_key': None,
        'priority': 0,
        'created_at': None,
        'updated_at': None,
        'dequeued_at': None,
        'completed_at': None,
        'executor_id': None,
        'app_version': None,
        'schedule_name': None,
        'deduplication_id': None,
        'parent_workflow_id': None,
        'forked_from': None,
        'input': None,
        'output': None,
        'error': None,
    }
    return SimpleNamespace(**(values | overrides))


def _mock_client(mocker: Any, status: SimpleNamespace | None = None, steps: list[dict[str, Any]] | None = None) -> Any:
    client = mocker.MagicMock()
    client.retrieve_workflow.return_value.get_status.return_value = status
    client.list_workflow_steps.return_value = steps or []
    client.list_workflows.return_value = []
    mocker.patch('django_dbos.admin_views.get_client', return_value=client)
    return client


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

    @pytest.mark.parametrize('path', ['/admin/dbos/workflows/', '/admin/dbos/schedules/'])
    def test_pages_render_with_the_admin_site_context(self, admin_client, dbos_environment, path):
        response = admin_client.get(path)
        # The site context brings the user tools (log out, theme toggle) and the sidebar.
        assert response.context['has_permission'] is True
        assert 'available_apps' in response.context
        assert_text(response, 'id="user-tools"')
        assert_text(response, '<ol class="breadcrumbs">')

    def test_workflow_list_shows_enqueued_task(self, admin_client, dbos_environment):
        result = _enqueue_sample_workflow()
        response = admin_client.get('/admin/dbos/workflows/')
        assert response.status_code == 200
        assert_text(response, 'tests.tasks.add')
        assert_text(response, result.id)

    def test_workflow_list_filters_by_task_path(self, admin_client, dbos_environment):
        result = _enqueue_sample_workflow()
        # The task's module path finds its workflow without django-dbos' prefix.
        response = admin_client.get('/admin/dbos/workflows/', {'name': 'tests.tasks.add'})
        assert result.id in [row['workflow_id'] for row in response.context['rows']]

    def test_workflow_list_filters_by_schedule(self, admin_client, mocker):
        client = _mock_client(mocker)
        admin_client.get('/admin/dbos/workflows/', {'schedule': 'nightly', 'name': 'django_dbos.tasks.x'})
        kwargs = client.list_workflows.call_args.kwargs
        assert (kwargs['schedule_name'], kwargs['name']) == ('nightly', 'django_dbos.tasks.x')

    @pytest.mark.parametrize(
        ('query', 'has_parent'),
        [
            pytest.param({}, False, id='hidden-by-default'),
            pytest.param({'children': '1'}, None, id='shown-on-demand'),
            pytest.param({'q': 'sched-'}, None, id='id-search-finds-children'),
        ],
    )
    def test_workflow_list_hides_child_workflows(self, admin_client, mocker, query, has_parent):
        client = _mock_client(mocker)
        client.list_workflows.return_value = [_workflow_status(parent_workflow_id='wf-parent')]
        response = admin_client.get('/admin/dbos/workflows/', query)
        assert client.list_workflows.call_args.kwargs['has_parent'] is has_parent
        assert response.context['rows'][0]['is_child'] is True
        assert_text(response, '>child</span>')

    @pytest.mark.parametrize(
        ('query', 'hinted'),
        [
            pytest.param({'children': '1', 'queue': 'default'}, True, id='children-and-queue'),
            pytest.param({'children': '1'}, False, id='children-only'),
            pytest.param({'queue': 'default'}, False, id='queue-only'),
        ],
    )
    def test_workflow_list_warns_that_children_have_no_queue(self, admin_client, mocker, query, hinted):
        _mock_client(mocker)
        response = admin_client.get('/admin/dbos/workflows/', query)
        assert ('the queue filter leaves them out' in response.content.decode()) is hinted

    def test_workflow_list_pagination_keeps_the_filters(self, admin_client, mocker):
        client = _mock_client(mocker)
        client.list_workflows.return_value = [_workflow_status(workflow_id=f'wf-{n}') for n in range(51)]
        response = admin_client.get('/admin/dbos/workflows/', {'status': 'SUCCESS', 'page': '2'})
        assert_text(response, 'href="?status=SUCCESS&amp;page=1"')
        assert_text(response, 'href="?status=SUCCESS&amp;page=3"')

    def test_pages_show_the_django_priority(self, admin_client, dbos_environment):
        # DBOS stores 101 - priority; the admin translates it back.
        result = add.using(priority=50).enqueue(2, 3)

        response = admin_client.get('/admin/dbos/workflows/')
        row = next(row for row in response.context['rows'] if row['workflow_id'] == result.id)
        assert row['priority'] == 50

        response = admin_client.get(f'/admin/dbos/workflows/{result.id}/')
        assert _identity(response)['Priority'] == 50

    def test_workflow_detail_shows_steps(self, admin_client, dbos_environment):
        result = wait_for_result(_enqueue_sample_workflow(), TaskResultStatus.SUCCESSFUL)
        response = admin_client.get(f'/admin/dbos/workflows/{result.id}/')
        assert response.status_code == 200
        assert_text(response, 'tests.tasks.add')
        assert_text(response, 'task_body')
        # docs/workflows.md points to this column to find fork_workflow's start_step.
        assert_text(response, '>ID</th>')

    def test_workflow_detail_renders_unusual_values(self, admin_client, mocker):
        # A step that never completed has no duration; outputs that are not
        # JSON-serializable fall back to str(); schedule-fired workflows may
        # carry ISO string timestamps.
        circular: list[Any] = []
        circular.append(circular)
        status = _workflow_status(
            workflow_id='wf-odd',
            name='tests.tasks.add',
            status='PENDING',
            queue_partition_key='user-42',
            created_at='2026-01-01T00:00:00+00:00',
            updated_at='not a timestamp',
            input=circular,
        )
        _mock_client(mocker, status, [{'function_id': 1, 'function_name': 'task_body', 'started_at_epoch_ms': 1_000}])

        response = admin_client.get('/admin/dbos/workflows/wf-odd/')

        assert response.status_code == 200
        (step,) = response.context['steps']
        assert (step['outcome'], step['duration_ms'], step['result']) == ('RUNNING', None, None)
        assert response.context['input'] == '[[...]]'
        assert_text(response, '2026-01-01 00:00:00')
        assert_text(response, 'not a timestamp')  # unparsable: shown as it is
        assert_text(response, 'user-42')
        assert_text(response, 'Not finished yet.')
        # DBOS priority 0 (not enqueued through the backend) has no Django priority: no field.
        assert 'Priority' not in _identity(response)

    def test_workflow_detail_links_related_workflows_and_times_the_run(self, admin_client, mocker):
        status = _workflow_status(
            name='django_dbos.schedules.tests.tasks.cron_task',
            schedule_name='django_dbos.schedules.tests.tasks.cron_task',
            parent_workflow_id='wf-parent',
            forked_from='wf-origin',
            created_at=1_000,
            dequeued_at=3_000,
            completed_at=8_000,
            output={'ok': True},
        )
        _mock_client(mocker, status)

        response = admin_client.get('/admin/dbos/workflows/wf-1/')

        assert response.context['title'] == 'tests.tasks.cron_task'
        assert response.context['row']['fired_by_cron'] is True
        assert_text(response, 'href="/admin/dbos/workflows/wf-parent/"')
        assert_text(response, 'href="/admin/dbos/workflows/wf-origin/"')
        assert_text(response, 'href="/admin/dbos/workflows/?schedule=django_dbos.schedules.tests.tasks.cron_task"')
        timing = {field['label']: field['value'] for field in response.context['timing']}
        assert (timing['Waited in queue'], timing['Run time']) == ('2.0 s', '5.0 s')

    @pytest.mark.parametrize(
        ('status', 'cancel', 'resume'),
        [
            pytest.param('SUCCESS', False, False, id='success'),
            pytest.param('ERROR', False, False, id='error'),
            pytest.param('PENDING', True, False, id='pending'),
            pytest.param('ENQUEUED', True, False, id='enqueued'),
            pytest.param('CANCELLED', False, True, id='cancelled'),
            pytest.param('MAX_RECOVERY_ATTEMPTS_EXCEEDED', False, True, id='dead-letter'),
        ],
    )
    def test_workflow_detail_offers_the_actions_of_its_status(self, admin_client, mocker, status, cancel, resume):
        _mock_client(mocker, _workflow_status(status=status))

        page = admin_client.get('/admin/dbos/workflows/wf-1/').content.decode()

        assert ('/admin/dbos/workflows/wf-1/cancel/' in page) is cancel
        assert ('/admin/dbos/workflows/wf-1/resume/' in page) is resume
        assert '/admin/dbos/workflows/wf-1/fork/' in page
        # Deleting goes through the confirmation page (a link, not a POST form).
        assert 'href="/admin/dbos/workflows/wf-1/delete/"' in page

    def test_workflow_detail_shows_the_error(self, admin_client, mocker):
        _mock_client(
            mocker,
            _workflow_status(status='ERROR', error=ValueError('card declined')),
            [
                {
                    'function_id': 1,
                    'function_name': 'task_body',
                    'started_at_epoch_ms': 1_000,
                    'completed_at_epoch_ms': 1_250,
                    'error': 'ValueError: boom',
                },
                {
                    'function_id': 2,
                    'function_name': 'report',
                    'started_at_epoch_ms': 1_000,
                    'completed_at_epoch_ms': 1_250,
                    'output': 'x' * 100,
                },
            ],
        )

        response = admin_client.get('/admin/dbos/workflows/wf-1/')

        failed, long_output = response.context['steps']
        assert (failed['outcome'], failed['result'], failed['result_is_error']) == ('ERROR', 'ValueError: boom', True)
        assert (long_output['outcome'], long_output['result_is_long']) == ('SUCCESS', True)
        assert len(long_output['result_preview']) == STEP_RESULT_INLINE_LENGTH
        assert long_output['result_preview'].endswith('…')
        assert_text(response, 'ValueError: card declined')
        assert_text(response, 'dbos-card-error')
        assert 'Output</h2>' not in response.content.decode()

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

    def test_schedule_rows(self, admin_client, mocker):
        client = _mock_client(mocker)
        client.list_schedules.return_value = [
            {
                'schedule_name': 'django_dbos.schedules.tests.tasks.cron_task',
                'workflow_name': 'django_dbos.schedules.tests.tasks.cron_task',
                'schedule': '*/10 * * * *',
                'status': 'ACTIVE',
                'last_fired_at': '2026-10-26T19:31:30+00:00',
            },
            {'schedule_name': 'nightly', 'workflow_name': 'other.workflow', 'status': 'PAUSED'},
        ]

        response = admin_client.get('/admin/dbos/schedules/')

        cron, nightly = response.context['rows']
        assert (cron['short_name'], cron['workflow_name']) == ('tests.tasks.cron_task', None)
        assert (nightly['short_name'], nightly['workflow_name']) == ('nightly', 'other.workflow')
        assert_text(response, '2026-10-26 19:31:30')
        assert_text(response, 'href="/admin/dbos/workflows/?schedule=django_dbos.schedules.tests.tasks.cron_task"')
        assert_text(response, 'href="/admin/dbos/schedules/nightly/delete/"')

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
            # reported by django_dbos.E001; ready() must not crash on it
            pytest.param('false', True, id='invalid-keeps-the-default'),
        ],
    )
    def test_admin_enabled_setting(self, settings, admin_enabled, expected):
        dj = {k: v for k, v in settings.TASKS['dbos']['OPTIONS'].items() if k != 'ADMIN_ENABLED'}
        if admin_enabled is not None:
            dj['ADMIN_ENABLED'] = admin_enabled
        set_dbos_options(settings, dj)
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
        set_dbos_options(settings, {**settings.TASKS['dbos']['OPTIONS'], 'ADMIN_ENABLED': False})
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

    @pytest.mark.parametrize('page', ['/admin/', '/admin/django_dbos/'])
    def test_superuser_app_list_links_the_dbos_pages(self, admin_client, page):
        response = admin_client.get(page)
        assert response.status_code == 200
        assert_text(response, 'href="/admin/dbos/workflows/"')
        assert_text(response, 'href="/admin/dbos/schedules/"')
        (app,) = [app for app in response.context['app_list'] if app['app_label'] == 'django_dbos']
        assert [entry['name'] for entry in app['models']] == ['Schedules', 'Worker heartbeats', 'Workflows']

    def test_staff_without_superuser_sees_no_dbos_links(self, db):
        staff = User.objects.create_user(username='editor', password='hunter2', is_staff=True)
        client = Client()
        client.force_login(staff)
        try:
            response = client.get('/admin/')
            assert response.status_code == 200
            assert '/admin/dbos/' not in response.content.decode()
        finally:
            User.objects.filter(pk=staff.pk).delete()

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
        write_heartbeat('machine-1', heartbeat_id=uuid.uuid4(), queues=('default',))
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
        set_dbos_options(settings, {'ADMIN_SITE': 'django.contrib.admin.site'})
        assert get_admin_site_paths() == ('django.contrib.admin.site',)

    def test_unknown_custom_site_raises(self, settings):
        set_dbos_options(settings, {'ADMIN_SITE': 'myproject.sites.missing_site'})
        with pytest.raises(ImportError):
            get_admin_sites()

    def test_path_to_a_non_admin_site_raises(self, settings):
        set_dbos_options(settings, {'ADMIN_SITE': 'django.contrib.admin.ModelAdmin'})
        with pytest.raises(TypeError, match='does not point to an AdminSite instance'):
            get_admin_sites()

    def test_heartbeat_model_already_registered_is_kept(self, dbos_environment):
        # A project may register WorkerHeartbeat with its own ModelAdmin first.
        site = AdminSite(name='preregistered-test')
        custom_admin = type('CustomHeartbeatAdmin', (WorkerHeartbeatAdmin,), {})
        site.register(WorkerHeartbeat, custom_admin)

        register_admin(site)

        assert type(site.get_model_admin(WorkerHeartbeat)) is custom_admin
        url_names = [p.name for p in site.get_urls() if getattr(p, 'name', None)]
        assert 'django_dbos_workflow_changelist' in url_names
