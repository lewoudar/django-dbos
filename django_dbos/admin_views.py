"""Admin views for django-dbos: workflows, steps, schedules and actions.

These are custom admin pages (not ModelAdmin): they read and act on the DBOS
system database through the process-wide ``DBOSClient`` — the web process
never launches DBOS itself, so it never executes workflows.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from dbos import StepInfo, WorkflowSchedule, WorkflowStatus, WorkflowStatusString
from dbos import error as dbos_error
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.text import Truncator

from .backends import _dbos_to_django_priority
from .client import get_client
from .exceptions import ImproperlyConfiguredDbos, describe_error, system_database_errors
from .logging import logger
from .scheduling import SCHEDULE_PREFIX
from .templatetags.dbos_admin import dbos_duration, dbos_time
from .workflow_names import REGISTRY_PREFIX

if TYPE_CHECKING:
    from django.contrib.admin import AdminSite

View = Callable[..., HttpResponse]

PAGE_SIZE = 50

# Step results up to this length, on one line, are shown inline; longer ones fold.
STEP_RESULT_INLINE_LENGTH = 80

# django-dbos' own workflow names: a task's module path behind one of these.
NAME_PREFIXES = (REGISTRY_PREFIX, SCHEDULE_PREFIX)

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

# The statuses each action applies to; DBOS ignores the others (resuming a
# SUCCESS or ERROR workflow does nothing), so the buttons are not offered.
CANCELLABLE_STATUSES = frozenset(
    {
        WorkflowStatusString.ENQUEUED.value,
        WorkflowStatusString.DELAYED.value,
        WorkflowStatusString.PENDING.value,
    }
)
RESUMABLE_STATUSES = frozenset(
    {WorkflowStatusString.CANCELLED.value, WorkflowStatusString.MAX_RECOVERY_ATTEMPTS_EXCEEDED.value}
)


def is_active_superuser(request: HttpRequest) -> bool:
    """Whether the request comes from an active superuser, the only users allowed on django-dbos' pages.

    A custom user model without ``PermissionsMixin`` has no ``is_superuser``
    field: its users are never superusers.

    Args:
        request: The current request, authenticated by ``AuthenticationMiddleware``.

    Returns:
        ``True`` when ``request.user`` is active and has the superuser flag.
    """
    user = request.user
    return bool(user.is_active and getattr(user, 'is_superuser', False))


def bind_site(view: View, site: AdminSite) -> View:
    """``view`` serving the admin ``site``: rendered with its context, URLs resolved in its namespace.

    Args:
        view: The django-dbos view to serve.
        site: The admin site serving it.

    Returns:
        The view, recording ``site`` on each request before calling it.
    """

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


def _dbos_view(view: View) -> View:
    """Wrap an admin view: require the superuser, render an error page when DBOS is unavailable.

    ``site.admin_view`` already enforces staff-only access; these pages also
    expose destructive operations and every workflow's data, so they are
    restricted to superusers. A misconfigured or unreachable system database
    renders ``error.html`` with a credential-free message instead of a 500.
    """

    @functools.wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not is_active_superuser(request):
            raise PermissionDenied('django-dbos admin pages are restricted to superusers.')
        try:
            get_client()
            return view(request, *args, **kwargs)
        except (ImproperlyConfiguredDbos, *system_database_errors()) as exc:
            message = describe_error(exc)
            logger.error('admin: DBOS system database unavailable', error=message, view=view.__name__)
            return _render(request, 'admin/django_dbos/error.html', {'title': 'django-dbos', 'error_message': message})

    return wrapper


def _pretty(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)


def _pretty_error(value: Any) -> str | None:
    """A workflow or step error: DBOS hands back the raised exception, shown as ``Type: message``."""
    if isinstance(value, BaseException):
        return f'{type(value).__name__}: {value}'
    if isinstance(value, str):
        return value
    return _pretty(value)


def _epoch_ms(value: int | str | None) -> datetime | str | None:
    if value is None:
        return None
    if isinstance(value, str):  # schedules store timestamps as ISO strings
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return value
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _elapsed_ms(start: int | None, end: int | None) -> int | None:
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    return end - start


def short_name(name: str | None) -> str | None:
    """A workflow name without django-dbos' prefix: the task's module path.

    Args:
        name: A workflow or schedule name.

    Returns:
        ``name`` without its django-dbos prefix; any other name (or ``None``) as it is.
    """
    for prefix in NAME_PREFIXES:
        if name and name.startswith(prefix):
            return name.removeprefix(prefix)
    return name


def _name_filter(name: str | None) -> str | list[str] | None:
    """The workflow names matching a name filter, which may omit django-dbos' prefix."""
    if not name or name.startswith(NAME_PREFIXES):
        return name
    return [name, *(prefix + name for prefix in NAME_PREFIXES)]


def _row(status: WorkflowStatus) -> dict[str, Any]:
    dbos_status = status.status
    return {
        'workflow_id': status.workflow_id,
        'name': status.name,
        'short_name': short_name(status.name),
        'fired_by_cron': bool(status.name and status.name.startswith(SCHEDULE_PREFIX)),
        'is_child': status.parent_workflow_id is not None,
        'status': dbos_status,
        'badge': STATUS_BADGES.get(dbos_status, 'neutral'),
        'queue_name': status.queue_name,
        'priority': _dbos_to_django_priority(status.priority),
        'created_at': _epoch_ms(status.created_at),
        'updated_at': _epoch_ms(status.updated_at),
        # Run time: from the dequeue (or the start, for an unqueued workflow) to completion.
        'duration_ms': _elapsed_ms(status.dequeued_at or status.created_at, status.completed_at),
        'error': status.error,
    }


# Cap on the page query parameter: an unbounded offset overflows database
# integers. 1000 pages of 50 reach the 50,000 newest workflows; older ones are
# found through the filters or an ID search.
MAX_PAGE = 1000


def _page_number(value: str | None) -> int:
    """Parse a page query parameter, clamped to a sane range."""
    try:
        return min(max(int(value or ''), 1), MAX_PAGE)
    except ValueError:
        return 1


@_dbos_view
def workflow_list(request: HttpRequest) -> HttpResponse:
    """The workflow list: newest first, filtered and paginated by the query parameters.

    Query parameters: ``status``, ``queue``, ``name`` (with or without
    django-dbos' prefix), ``schedule``, ``q`` (a workflow ID prefix),
    ``children=1`` (show child workflows) and ``page``.

    Args:
        request: The current request.

    Returns:
        The rendered list page.
    """
    status_filter = request.GET.get('status') or None
    queue_filter = request.GET.get('queue') or None
    name_filter = request.GET.get('name') or None
    schedule_filter = request.GET.get('schedule') or None
    search = request.GET.get('q') or None
    show_children = request.GET.get('children') == '1'
    page = _page_number(request.GET.get('page'))
    offset = (page - 1) * PAGE_SIZE

    workflows = get_client().list_workflows(
        status=status_filter,
        queue_name=queue_filter,
        name=_name_filter(name_filter),
        schedule_name=schedule_filter,
        workflow_id_prefix=search,
        # Child workflows are hidden by default: each @cron firing is a schedule
        # workflow running its task as a child, so it would show up twice. An
        # ID search still finds a child.
        has_parent=None if show_children or search else False,
        limit=PAGE_SIZE + 1,
        offset=offset,
        sort_desc=True,
        load_input=False,
        load_output=False,
    )
    # No link past the cap: it would clamp back to this very page.
    has_next = len(workflows) > PAGE_SIZE and page < MAX_PAGE
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
                'schedule': schedule_filter or '',
                'q': search or '',
                'children': show_children,
            },
            'page': page,
            'has_previous': page > 1,
            'has_next': has_next,
        },
    )


def _get_workflow_or_404(workflow_id: str) -> WorkflowStatus:
    try:
        return get_client().retrieve_workflow(workflow_id).get_status()
    except dbos_error.DBOSNonExistentWorkflowError:
        raise Http404(f'No workflow found with id {workflow_id!r}') from None


def _field(label: str, value: Any, *, url: str | None = None, mono: bool = False) -> dict[str, Any]:
    return {'label': label, 'value': value, 'url': url, 'mono': mono}


def _admin_url(request: HttpRequest, name: str, *args: str) -> str:
    """The URL of a django-dbos admin page on the admin site serving ``request``.

    ``reverse()`` does not read ``request.current_app`` (only the ``{% url %}``
    tag does): without it, a page served by another site links to the default one.
    """
    return reverse(f'admin:django_dbos_{name}', args=args, current_app=request.current_app)


def _workflow_url(request: HttpRequest, workflow_id: str | None) -> str | None:
    return _admin_url(request, 'workflow_detail', workflow_id) if workflow_id else None


def _workflows_of_schedule_url(request: HttpRequest, schedule_name: str) -> str:
    return f'{_admin_url(request, "workflow_changelist")}?{urlencode({"schedule": schedule_name})}'


def _redirect_to_workflow(request: HttpRequest, workflow_id: str) -> HttpResponseRedirect:
    return HttpResponseRedirect(_admin_url(request, 'workflow_detail', workflow_id))


def _step_row(step: StepInfo) -> dict[str, Any]:
    error = _pretty_error(step['error'])
    if error is not None:
        outcome, badge = 'ERROR', 'error'
    elif step['completed_at_epoch_ms'] is not None:
        outcome, badge = 'SUCCESS', 'success'
    else:
        outcome, badge = 'RUNNING', 'info'
    result = error if error is not None else _pretty(step['output'])
    return {
        'function_id': step['function_id'],
        'name': step['function_name'],
        'outcome': outcome,
        'badge': badge,
        'duration_ms': _elapsed_ms(step['started_at_epoch_ms'], step['completed_at_epoch_ms']),
        # The error, or else the output; a long or multi-line one is folded in the page.
        'result': result,
        'result_is_error': error is not None,
        'result_is_long': result is not None and (len(result) > STEP_RESULT_INLINE_LENGTH or '\n' in result),
        # What a folded result shows before it is unfolded: as much as fits inline.
        'result_preview': Truncator(result).chars(STEP_RESULT_INLINE_LENGTH) if result is not None else None,
        'child_workflow_id': step['child_workflow_id'],
    }


@_dbos_view
def workflow_detail(request: HttpRequest, workflow_id: str) -> HttpResponse:
    """A workflow's page: identity, timing, input, output, error, steps and actions.

    Args:
        request: The current request.
        workflow_id: The workflow's ID.

    Returns:
        The rendered detail page.

    Raises:
        Http404: No workflow has this ID.
    """
    status = _get_workflow_or_404(workflow_id)
    row = _row(status)
    steps = [_step_row(step) for step in get_client().list_workflow_steps(workflow_id, load_output=True)]

    # Fields without a value are left out: most workflows have no partition
    # key, deduplication ID, schedule or parent.
    identity = [
        _field('Workflow ID', status.workflow_id, mono=True),
        _field('Workflow name', status.name, mono=True),
        _field('Queue', status.queue_name),
        _field('Partition key', status.queue_partition_key, mono=True),
        _field('Priority', row['priority']),
        _field('Deduplication ID', status.deduplication_id, mono=True),
        _field(
            'Schedule',
            short_name(status.schedule_name),
            url=_workflows_of_schedule_url(request, status.schedule_name) if status.schedule_name else None,
        ),
        _field(
            'Parent workflow',
            status.parent_workflow_id,
            url=_workflow_url(request, status.parent_workflow_id),
            mono=True,
        ),
        _field('Forked from', status.forked_from, url=_workflow_url(request, status.forked_from), mono=True),
        _field('Executor', status.executor_id, mono=True),
        _field('Application version', status.app_version, mono=True),
    ]
    timing = [
        _field('Created', dbos_time(_epoch_ms(status.created_at)) or None),
        _field('Dequeued', dbos_time(_epoch_ms(status.dequeued_at)) or None),
        _field('Completed', dbos_time(_epoch_ms(status.completed_at)) or None),
        _field('Last updated', dbos_time(_epoch_ms(status.updated_at)) or None),
        _field('Waited in queue', dbos_duration(_elapsed_ms(status.created_at, status.dequeued_at)) or None),
        _field('Run time', dbos_duration(row['duration_ms']) or None),
    ]

    return _render(
        request,
        'admin/django_dbos/workflow_detail.html',
        {
            'title': row['short_name'] or workflow_id,
            'row': row,
            'identity': [field for field in identity if field['value'] is not None],
            'timing': [field for field in timing if field['value'] is not None],
            'can_cancel': row['status'] in CANCELLABLE_STATUSES,
            'can_resume': row['status'] in RESUMABLE_STATUSES,
            'input': _pretty(status.input),
            'output': _pretty(status.output),
            'error': _pretty_error(status.error),
            'steps': steps,
        },
    )


@_dbos_view
def workflow_cancel(request: HttpRequest, workflow_id: str) -> HttpResponse:
    """Cancel a workflow on POST; any other method only redirects to its page.

    Args:
        request: The current request.
        workflow_id: The workflow's ID.

    Returns:
        A redirect to the workflow's page.

    Raises:
        Http404: No workflow has this ID.
    """
    _get_workflow_or_404(workflow_id)
    if request.method != 'POST':
        return _redirect_to_workflow(request, workflow_id)
    get_client().cancel_workflow(workflow_id)
    logger.info('admin: cancelled workflow', workflow_id=workflow_id, user=request.user.pk)
    return _redirect_to_workflow(request, workflow_id)


@_dbos_view
def workflow_resume(request: HttpRequest, workflow_id: str) -> HttpResponse:
    """Resume a cancelled or dead-lettered workflow on POST; any other method only redirects to its page.

    Args:
        request: The current request.
        workflow_id: The workflow's ID.

    Returns:
        A redirect to the workflow's page.

    Raises:
        Http404: No workflow has this ID.
    """
    _get_workflow_or_404(workflow_id)
    if request.method != 'POST':
        return _redirect_to_workflow(request, workflow_id)
    get_client().resume_workflow(workflow_id)
    logger.info('admin: resumed workflow', workflow_id=workflow_id, user=request.user.pk)
    return _redirect_to_workflow(request, workflow_id)


@_dbos_view
def workflow_fork(request: HttpRequest, workflow_id: str) -> HttpResponse:
    """Fork a workflow (a new run from its start) on POST; any other method only redirects to its page.

    Args:
        request: The current request.
        workflow_id: The workflow's ID.

    Returns:
        A redirect to the fork's page, or to the workflow's page without a POST.

    Raises:
        Http404: No workflow has this ID.
    """
    _get_workflow_or_404(workflow_id)
    if request.method != 'POST':
        return _redirect_to_workflow(request, workflow_id)
    handle = get_client().fork_workflow(workflow_id, 0)
    logger.info('admin: forked workflow', workflow_id=workflow_id, fork=handle.workflow_id, user=request.user.pk)
    return _redirect_to_workflow(request, handle.workflow_id)


@_dbos_view
def workflow_delete(request: HttpRequest, workflow_id: str) -> HttpResponse:
    """Delete a workflow on POST; any other method shows the confirmation page.

    Args:
        request: The current request.
        workflow_id: The workflow's ID.

    Returns:
        A redirect to the workflow list, or the rendered confirmation page.

    Raises:
        Http404: No workflow has this ID.
    """
    _get_workflow_or_404(workflow_id)
    if request.method == 'POST':
        get_client().delete_workflow(workflow_id)
        logger.info('admin: deleted workflow', workflow_id=workflow_id, user=request.user.pk)
        return HttpResponseRedirect(_admin_url(request, 'workflow_changelist'))

    return _render(
        request,
        'admin/django_dbos/delete_confirmation.html',
        {
            'title': f'Delete workflow {workflow_id}',
            'back_list_url': _admin_url(request, 'workflow_changelist'),
            'back_list_label': 'Workflows',
            'object_kind': 'workflow',
            'object_id': workflow_id,
            'warning': 'permanently removes its execution history and steps from the system database.',
            'back_url': _admin_url(request, 'workflow_detail', workflow_id),
        },
    )


# --- Schedules ------------------------------------------------------------


def _schedule_row(request: HttpRequest, schedule: WorkflowSchedule) -> dict[str, Any]:
    schedule_name = schedule['schedule_name']
    workflow_name = schedule['workflow_name']
    return {
        'schedule_name': schedule_name,
        'short_name': short_name(schedule_name),
        # @cron schedules run a workflow of the same name: only show a different one.
        'workflow_name': workflow_name if workflow_name != schedule_name else None,
        'workflows_url': _workflows_of_schedule_url(request, schedule_name),
        'crontab': schedule['schedule'],
        'status': schedule['status'],
        'queue_name': schedule['queue_name'],
        'cron_timezone': schedule['cron_timezone'],
        'last_fired_at': _epoch_ms(schedule['last_fired_at']),
    }


@_dbos_view
def schedule_list(request: HttpRequest) -> HttpResponse:
    """The schedule list, with each schedule's crontab, status and last firing.

    Args:
        request: The current request.

    Returns:
        The rendered list page.
    """
    rows = [_schedule_row(request, schedule) for schedule in get_client().list_schedules()]
    return _render(
        request,
        'admin/django_dbos/schedule_list.html',
        {'title': 'DBOS schedules', 'rows': rows},
    )


def _redirect_to_schedules(request: HttpRequest) -> HttpResponseRedirect:
    return HttpResponseRedirect(_admin_url(request, 'schedule_changelist'))


def _get_schedule_or_404(schedule_name: str) -> WorkflowSchedule:
    schedule = get_client().get_schedule(schedule_name)
    if schedule is None:
        raise Http404(f'No schedule found with name {schedule_name!r}')
    return schedule


@_dbos_view
def schedule_pause(request: HttpRequest, schedule_name: str) -> HttpResponse:
    """Pause a schedule on POST; any other method only redirects to the list.

    Args:
        request: The current request.
        schedule_name: The schedule's name.

    Returns:
        A redirect to the schedule list.

    Raises:
        Http404: No schedule has this name.
    """
    _get_schedule_or_404(schedule_name)
    if request.method == 'POST':
        get_client().pause_schedule(schedule_name)
        logger.info('admin: paused schedule', schedule_name=schedule_name, user=request.user.pk)
    return _redirect_to_schedules(request)


@_dbos_view
def schedule_resume(request: HttpRequest, schedule_name: str) -> HttpResponse:
    """Resume a paused schedule on POST; any other method only redirects to the list.

    Args:
        request: The current request.
        schedule_name: The schedule's name.

    Returns:
        A redirect to the schedule list.

    Raises:
        Http404: No schedule has this name.
    """
    _get_schedule_or_404(schedule_name)
    if request.method == 'POST':
        get_client().resume_schedule(schedule_name)
        logger.info('admin: resumed schedule', schedule_name=schedule_name, user=request.user.pk)
    return _redirect_to_schedules(request)


@_dbos_view
def schedule_delete(request: HttpRequest, schedule_name: str) -> HttpResponse:
    """Delete a schedule on POST; any other method shows the confirmation page.

    Args:
        request: The current request.
        schedule_name: The schedule's name.

    Returns:
        A redirect to the schedule list, or the rendered confirmation page.

    Raises:
        Http404: No schedule has this name.
    """
    _get_schedule_or_404(schedule_name)
    if request.method == 'POST':
        get_client().delete_schedule(schedule_name)
        logger.info('admin: deleted schedule', schedule_name=schedule_name, user=request.user.pk)
        return _redirect_to_schedules(request)

    return _render(
        request,
        'admin/django_dbos/delete_confirmation.html',
        {
            'title': f'Delete schedule {schedule_name}',
            'back_list_url': _admin_url(request, 'schedule_changelist'),
            'back_list_label': 'Schedules',
            'object_kind': 'schedule',
            'object_id': schedule_name,
            'warning': 'stops its cron firings (it is re-created on the next `dbos worker` start if the task is still scheduled).',
        },
    )
