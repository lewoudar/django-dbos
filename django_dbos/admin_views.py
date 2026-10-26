"""Admin views for django-dbos: workflows, steps, schedules and actions.

These are custom admin pages (not ModelAdmin): they read and act on the DBOS
system database through the process-wide ``DBOSClient`` — the web process
never launches DBOS itself, so it never executes workflows.
"""

from __future__ import annotations

import functools
import json
from datetime import UTC, datetime
from typing import Any

from dbos import WorkflowStatusString
from dbos import error as dbos_error
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse

from .backends import _dbos_to_django_priority
from .client import get_client
from .exceptions import ImproperlyConfiguredDbos, describe_error, system_database_errors
from .logging import logger

PAGE_SIZE = 50

# Workflow status -> CSS class for the status badge in templates.
STATUS_BADGES = {
    WorkflowStatusString.SUCCESS.value: 'success',
    WorkflowStatusString.ERROR.value: 'error',
    WorkflowStatusString.MAX_RECOVERY_ATTEMPTS_EXCEEDED.value: 'error',
    WorkflowStatusString.PENDING.value: 'info',  # DBOS' status of an executing workflow
    WorkflowStatusString.ENQUEUED.value: 'neutral',
    WorkflowStatusString.DELAYED.value: 'neutral',
    WorkflowStatusString.CANCELLED.value: 'warning',
}

WORKFLOW_STATUSES = list(STATUS_BADGES)


def bind_site(view: Any, site: Any) -> Any:
    """``view`` serving the admin ``site``: rendered with its context, URLs resolved in its namespace."""

    @functools.wraps(view)
    def inner(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        request.current_app = site.name
        request.django_dbos_admin_site = site  # type: ignore[attr-defined]
        return view(request, *args, **kwargs)

    return inner


def _render(request: HttpRequest, template_name: str, context: dict[str, Any]) -> HttpResponse:
    """Render with the admin site's context: header, user tools, navigation sidebar, theme toggle."""
    site_context = request.django_dbos_admin_site.each_context(request)  # type: ignore[attr-defined]
    return render(request, template_name, {**site_context, **context})


def _dbos_view(view: Any) -> Any:
    """Wrap an admin view: require the superuser, render an error page when DBOS is unavailable.

    ``site.admin_view`` already enforces staff-only access; these pages also
    expose destructive operations and every workflow's data, so they are
    restricted to superusers. A misconfigured or unreachable system database
    renders ``error.html`` with a credential-free message instead of a 500.
    """

    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not request.user.is_active or not request.user.is_superuser:
            raise PermissionDenied('django-dbos admin pages are restricted to superusers.')
        try:
            get_client()
            return view(request, *args, **kwargs)
        except (ImproperlyConfiguredDbos, *system_database_errors()) as exc:
            message = describe_error(exc)
            logger.error('admin: DBOS system database unavailable', error=message, view=view.__name__)
            return _render(request, 'admin/django_dbos/error.html', {'title': 'django-dbos', 'error_message': message})

    wrapper.__name__ = view.__name__
    wrapper.__qualname__ = view.__qualname__
    return wrapper


def _pretty(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)


def _epoch_ms(value: int | str | None) -> datetime | str | None:
    if value is None:
        return None
    if isinstance(value, str):  # schedules store timestamps as ISO strings
        return value
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _row(status: Any) -> dict[str, Any]:
    django_status = status.status.value if hasattr(status.status, 'value') else str(status.status)
    return {
        'workflow_id': status.workflow_id,
        'name': status.name,
        'status': django_status,
        'badge': STATUS_BADGES.get(django_status, 'neutral'),
        'queue_name': status.queue_name,
        'priority': _dbos_to_django_priority(status.priority),
        'created_at': _epoch_ms(status.created_at),
        'updated_at': _epoch_ms(status.updated_at),
        'error': status.error,
    }


# Cap on the page query parameter: beyond this there is no meaningful data
# (50 pages of 50 workflows), and unbounded offsets overflow database integers.
_MAX_PAGE = 50


def _page_number(value: str | None) -> int:
    """Parse a page query parameter, clamped to a sane range."""
    try:
        return min(max(int(value or ''), 1), _MAX_PAGE)
    except ValueError:
        return 1


@_dbos_view
def workflow_list(request: HttpRequest) -> HttpResponse:
    status_filter = request.GET.get('status') or None
    queue_filter = request.GET.get('queue') or None
    name_filter = request.GET.get('name') or None
    search = request.GET.get('q') or None
    page = _page_number(request.GET.get('page'))
    offset = (page - 1) * PAGE_SIZE

    workflows = get_client().list_workflows(
        status=status_filter,
        queue_name=queue_filter,
        name=name_filter,
        workflow_id_prefix=search,
        limit=PAGE_SIZE + 1,
        offset=offset,
        sort_desc=True,
        load_input=False,
        load_output=False,
    )
    has_next = len(workflows) > PAGE_SIZE
    rows = [_row(status) for status in workflows[:PAGE_SIZE]]

    return _render(
        request,
        'admin/django_dbos/workflow_list.html',
        {
            'title': 'DBOS workflows',
            'rows': rows,
            'statuses': WORKFLOW_STATUSES,
            'filters': {
                'status': status_filter or '',
                'queue': queue_filter or '',
                'name': name_filter or '',
                'q': search or '',
            },
            'page': page,
            'has_previous': page > 1,
            'has_next': has_next,
        },
    )


def _get_workflow_or_404(workflow_id: str) -> Any:
    try:
        return get_client().retrieve_workflow(workflow_id).get_status()
    except dbos_error.DBOSNonExistentWorkflowError:
        raise Http404(f'No workflow found with id {workflow_id!r}') from None


@_dbos_view
def workflow_detail(request: HttpRequest, workflow_id: str) -> HttpResponse:
    status = _get_workflow_or_404(workflow_id)
    django_status = status.status.value if hasattr(status.status, 'value') else str(status.status)

    steps = []
    for step in get_client().list_workflow_steps(workflow_id, load_output=True):
        duration_ms = None
        if step.get('started_at_epoch_ms') is not None and step.get('completed_at_epoch_ms') is not None:
            duration_ms = step['completed_at_epoch_ms'] - step['started_at_epoch_ms']
        steps.append(
            {
                'function_id': step.get('function_id'),
                'name': step.get('function_name'),
                'duration_ms': duration_ms,
                'output': _pretty(step.get('output')),
                'error': _pretty(step.get('error')),
                'child_workflow_id': step.get('child_workflow_id'),
            }
        )

    return _render(
        request,
        'admin/django_dbos/workflow_detail.html',
        {
            'title': f'DBOS workflow {workflow_id}',
            'row': _row(status),
            'fields': [
                ('Workflow ID', status.workflow_id),
                ('Name', status.name),
                ('Status', django_status),
                ('Queue', status.queue_name),
                ('Partition key', status.queue_partition_key),
                ('Priority', _dbos_to_django_priority(status.priority)),
                ('Created at', _epoch_ms(status.created_at)),
                ('Updated at', _epoch_ms(status.updated_at)),
                ('Completed at', _epoch_ms(status.completed_at)),
                ('Executor', status.executor_id),
                ('Application version', status.app_version),
                ('Schedule', status.schedule_name),
                ('Deduplication ID', status.deduplication_id),
            ],
            'input': _pretty(status.input),
            'output': _pretty(status.output),
            'error': _pretty(status.error),
            'steps': steps,
        },
    )


@_dbos_view
def workflow_cancel(request: HttpRequest, workflow_id: str) -> HttpResponse:
    if request.method != 'POST':
        return HttpResponseRedirect(reverse('admin:django_dbos_workflow_detail', args=[workflow_id]))
    _get_workflow_or_404(workflow_id)
    get_client().cancel_workflow(workflow_id)
    logger.info('admin: cancelled workflow', workflow_id=workflow_id, user=request.user.pk)
    return _redirect_to_workflow(workflow_id)


@_dbos_view
def workflow_resume(request: HttpRequest, workflow_id: str) -> HttpResponse:
    if request.method != 'POST':
        return HttpResponseRedirect(reverse('admin:django_dbos_workflow_detail', args=[workflow_id]))
    _get_workflow_or_404(workflow_id)
    get_client().resume_workflow(workflow_id)
    logger.info('admin: resumed workflow', workflow_id=workflow_id, user=request.user.pk)
    return _redirect_to_workflow(workflow_id)


@_dbos_view
def workflow_fork(request: HttpRequest, workflow_id: str) -> HttpResponse:
    if request.method != 'POST':
        return HttpResponseRedirect(reverse('admin:django_dbos_workflow_detail', args=[workflow_id]))
    _get_workflow_or_404(workflow_id)
    handle = get_client().fork_workflow(workflow_id, 0)
    logger.info('admin: forked workflow', workflow_id=workflow_id, fork=handle.workflow_id, user=request.user.pk)
    return _redirect_to_workflow(handle.workflow_id)


@_dbos_view
def workflow_delete(request: HttpRequest, workflow_id: str) -> HttpResponse:
    _get_workflow_or_404(workflow_id)
    if request.method == 'POST':
        get_client().delete_workflow(workflow_id)
        logger.info('admin: deleted workflow', workflow_id=workflow_id, user=request.user.pk)
        return HttpResponseRedirect(reverse('admin:django_dbos_workflow_changelist'))

    return _render(
        request,
        'admin/django_dbos/delete_confirmation.html',
        {
            'title': f'Delete workflow {workflow_id}',
            'back_list_url': reverse('admin:django_dbos_workflow_changelist'),
            'back_list_label': 'DBOS workflows',
            'object_kind': 'workflow',
            'object_id': workflow_id,
            'warning': 'permanently removes its execution history and steps from the system database.',
            'back_url': reverse('admin:django_dbos_workflow_detail', args=[workflow_id]),
        },
    )


# --- Schedules ------------------------------------------------------------


def _schedule_row(schedule: dict[str, Any]) -> dict[str, Any]:
    return {
        'schedule_name': schedule.get('schedule_name'),
        'workflow_name': schedule.get('workflow_name'),
        'crontab': schedule.get('schedule'),
        'status': schedule.get('status'),
        'queue_name': schedule.get('queue_name'),
        'cron_timezone': schedule.get('cron_timezone'),
        'last_fired_at': _epoch_ms(schedule.get('last_fired_at')),
    }


@_dbos_view
def schedule_list(request: HttpRequest) -> HttpResponse:
    rows = [_schedule_row(schedule) for schedule in get_client().list_schedules()]
    return _render(
        request,
        'admin/django_dbos/schedule_list.html',
        {'title': 'DBOS schedules', 'rows': rows},
    )


def _redirect_to_schedules() -> HttpResponseRedirect:
    return HttpResponseRedirect(reverse('admin:django_dbos_schedule_changelist'))


def _get_schedule_or_404(schedule_name: str) -> Any:
    schedule = get_client().get_schedule(schedule_name)
    if schedule is None:
        raise Http404(f'No schedule found with name {schedule_name!r}')
    return schedule


@_dbos_view
def schedule_pause(request: HttpRequest, schedule_name: str) -> HttpResponse:
    _get_schedule_or_404(schedule_name)
    if request.method == 'POST':
        get_client().pause_schedule(schedule_name)
        logger.info('admin: paused schedule', schedule_name=schedule_name, user=request.user.pk)
    return _redirect_to_schedules()


@_dbos_view
def schedule_resume(request: HttpRequest, schedule_name: str) -> HttpResponse:
    _get_schedule_or_404(schedule_name)
    if request.method == 'POST':
        get_client().resume_schedule(schedule_name)
        logger.info('admin: resumed schedule', schedule_name=schedule_name, user=request.user.pk)
    return _redirect_to_schedules()


@_dbos_view
def schedule_delete(request: HttpRequest, schedule_name: str) -> HttpResponse:
    _get_schedule_or_404(schedule_name)
    if request.method == 'POST':
        get_client().delete_schedule(schedule_name)
        logger.info('admin: deleted schedule', schedule_name=schedule_name, user=request.user.pk)
        return _redirect_to_schedules()

    return _render(
        request,
        'admin/django_dbos/delete_confirmation.html',
        {
            'title': f'Delete schedule {schedule_name}',
            'back_list_url': reverse('admin:django_dbos_schedule_changelist'),
            'back_list_label': 'DBOS schedules',
            'object_kind': 'schedule',
            'object_id': schedule_name,
            'warning': 'stops its cron firings (it is re-created on the next `dbos worker` start if the task is still scheduled).',
        },
    )


def _redirect_to_workflow(workflow_id: str) -> HttpResponseRedirect:
    return HttpResponseRedirect(reverse('admin:django_dbos_workflow_detail', args=[workflow_id]))
