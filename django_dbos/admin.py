"""Registration of django-dbos pages in the Django admin.

Custom admin views: workflow list/detail, schedules, and operational actions,
all backed by a ``DBOSClient`` against the system database — plus a read-only
``ModelAdmin`` for worker heartbeats.

Registration is gated by ``DJANGO_DBOS["ADMIN_ENABLED"]`` (default ``True``)
and targets the admin sites listed in ``DJANGO_DBOS["ADMIN_SITE"]`` (dotted
paths, default: the default ``django.contrib.admin.site``).

The pages expose destructive operations and every workflow's data (inputs,
outputs, errors), so they additionally require the superuser flag — the
standard staff membership is not enough.
"""

from __future__ import annotations

from typing import Any

from django.contrib import admin as django_admin
from django.contrib.admin import AdminSite
from django.urls import URLPattern, path
from django.utils.module_loading import import_string

from . import admin_views
from .logging import logger
from .models import WorkerHeartbeat
from .settings import get_dbos_setting

APP_LABEL = 'django_dbos'
ADMIN_URL_PREFIX = 'dbos/'
DEFAULT_ADMIN_SITES = ('django.contrib.admin.site',)


def is_admin_enabled() -> bool:
    return bool(get_dbos_setting('ADMIN_ENABLED', True))


def get_admin_site_paths() -> tuple[str, ...]:
    sites = get_dbos_setting('ADMIN_SITE') or DEFAULT_ADMIN_SITES
    if isinstance(sites, str):
        sites = (sites,)
    return tuple(sites)


def get_admin_sites() -> list[Any]:
    """Resolve the configured AdminSite instances.

    When the default site is requested but ``django.contrib.admin`` is not
    installed, it is silently skipped (projects without an admin at all).
    """
    configured_paths = get_admin_site_paths()
    resolved: list[Any] = []
    for dotted_path in configured_paths:
        try:
            site = import_string(dotted_path)
            # isinstance on the lazy object forces AdminConfig.setup(), which
            # fails when django.contrib.admin is not in INSTALLED_APPS.
            if not isinstance(site, AdminSite):
                raise TypeError(f'{dotted_path!r} does not point to an AdminSite instance')
        except (ImportError, LookupError):
            if configured_paths == DEFAULT_ADMIN_SITES:
                continue
            raise
        resolved.append(site)
    return resolved


class WorkerHeartbeatAdmin(django_admin.ModelAdmin):
    """Read-only view of the live workers (superusers only, like the DBOS pages)."""

    list_display = ('worker_id', 'executor_id', 'hostname', 'pid', 'cpu_percent', 'memory_rss_mb', 'last_seen_at')
    list_filter = ('executor_id',)
    ordering = ('executor_id', 'hostname', 'pid')

    def has_add_permission(self, request: Any) -> bool:
        return False

    def has_change_permission(self, request: Any, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: Any, obj: Any = None) -> bool:
        return bool(request.user.is_superuser)

    def has_view_permission(self, request: Any, obj: Any = None) -> bool:
        return bool(request.user.is_superuser)


def register_admin(site: Any) -> None:
    """Append django-dbos URLs to an AdminSite instance (idempotent)."""
    if getattr(site, '_django_dbos_registered', False):
        return

    if not site.is_registered(WorkerHeartbeat):
        site.register(WorkerHeartbeat, WorkerHeartbeatAdmin)

    original_get_urls = site.get_urls

    def get_urls() -> list[URLPattern]:
        urls = original_get_urls()
        dbos_urls = [
            path(
                f'{ADMIN_URL_PREFIX}workflows/',
                site.admin_view(admin_views.workflow_list),
                name='django_dbos_workflow_changelist',
            ),
            path(
                f'{ADMIN_URL_PREFIX}workflows/<str:workflow_id>/',
                site.admin_view(admin_views.workflow_detail),
                name='django_dbos_workflow_detail',
            ),
            path(
                f'{ADMIN_URL_PREFIX}workflows/<str:workflow_id>/cancel/',
                site.admin_view(admin_views.workflow_cancel),
                name='django_dbos_workflow_cancel',
            ),
            path(
                f'{ADMIN_URL_PREFIX}workflows/<str:workflow_id>/resume/',
                site.admin_view(admin_views.workflow_resume),
                name='django_dbos_workflow_resume',
            ),
            path(
                f'{ADMIN_URL_PREFIX}workflows/<str:workflow_id>/fork/',
                site.admin_view(admin_views.workflow_fork),
                name='django_dbos_workflow_fork',
            ),
            path(
                f'{ADMIN_URL_PREFIX}workflows/<str:workflow_id>/delete/',
                site.admin_view(admin_views.workflow_delete),
                name='django_dbos_workflow_delete',
            ),
            path(
                f'{ADMIN_URL_PREFIX}schedules/',
                site.admin_view(admin_views.schedule_list),
                name='django_dbos_schedule_changelist',
            ),
            path(
                f'{ADMIN_URL_PREFIX}schedules/<str:schedule_name>/pause/',
                site.admin_view(admin_views.schedule_pause),
                name='django_dbos_schedule_pause',
            ),
            path(
                f'{ADMIN_URL_PREFIX}schedules/<str:schedule_name>/resume/',
                site.admin_view(admin_views.schedule_resume),
                name='django_dbos_schedule_resume',
            ),
            path(
                f'{ADMIN_URL_PREFIX}schedules/<str:schedule_name>/delete/',
                site.admin_view(admin_views.schedule_delete),
                name='django_dbos_schedule_delete',
            ),
        ]
        # Prepend: the admin's own generic patterns (app_label/model_name)
        # would otherwise swallow paths like "dbos/workflows/".
        return dbos_urls + urls

    site.get_urls = get_urls
    site._django_dbos_registered = True
    logger.debug('registered django-dbos admin views', admin_site=site.name)


def register_admin_sites() -> None:
    """Register admin views on every configured AdminSite (called on ready)."""
    if not is_admin_enabled():
        return
    for site in get_admin_sites():
        register_admin(site)
