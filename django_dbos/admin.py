"""Registration of django-dbos pages in the Django admin.

Custom admin views: workflow list/detail, schedules, and operational actions,
all backed by a ``DBOSClient`` against the system database — plus a read-only
``ModelAdmin`` for worker heartbeats.

Registration is gated by ``OPTIONS["ADMIN_ENABLED"]`` (default ``True``)
and targets the admin sites listed in ``OPTIONS["ADMIN_SITE"]`` (dotted
paths, default: the default ``django.contrib.admin.site``).

The pages expose destructive operations and every workflow's data (inputs,
outputs, errors), so they additionally require the superuser flag — the
standard staff membership is not enough.
"""

from __future__ import annotations

import weakref
from typing import Any

from django.contrib import admin as django_admin
from django.contrib.admin import AdminSite
from django.http import HttpRequest
from django.urls import URLPattern, URLResolver, path, reverse
from django.utils.module_loading import import_string

from . import admin_views
from .exceptions import ImproperlyConfiguredDbos
from .logging import logger
from .models import WorkerHeartbeat
from .settings import get_bool_option, get_dbos_setting

APP_LABEL = 'django_dbos'
ADMIN_URL_PREFIX = 'dbos/'
DEFAULT_ADMIN_SITES = ('django.contrib.admin.site',)

# Sites register_admin() already patched: registration is idempotent.
_registered_sites: weakref.WeakSet[AdminSite] = weakref.WeakSet()


def is_admin_enabled() -> bool:
    try:
        return get_bool_option('ADMIN_ENABLED')
    except ImproperlyConfiguredDbos:
        # Read by ready(), before the system checks: an invalid value keeps the
        # default and is reported by django_dbos.E001 rather than crashing every command.
        return True


def get_admin_site_paths() -> tuple[str, ...]:
    sites = get_dbos_setting('ADMIN_SITE') or DEFAULT_ADMIN_SITES
    if isinstance(sites, str):
        sites = (sites,)
    return tuple(sites)


def get_admin_sites() -> list[AdminSite]:
    """Resolve the configured AdminSite instances.

    When the default site is requested but ``django.contrib.admin`` is not
    installed, it is silently skipped (projects without an admin at all).
    """
    configured_paths = get_admin_site_paths()
    resolved: list[AdminSite] = []
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

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: WorkerHeartbeat | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: WorkerHeartbeat | None = None) -> bool:
        return admin_views.is_active_superuser(request)

    def has_view_permission(self, request: HttpRequest, obj: WorkerHeartbeat | None = None) -> bool:
        return admin_views.is_active_superuser(request)


def _dbos_page_entries(site: AdminSite) -> list[dict[str, Any]]:
    """App-list entries linking the workflow and schedule pages, shaped like the admin's model entries.

    They sit next to the worker heartbeats in the "django_dbos" app, on the
    admin index, the app page and the navigation sidebar. The app is present
    for superusers since ``register_admin`` registers ``WorkerHeartbeat``.
    """
    pages = (('Workflows', 'DbosWorkflow', 'workflow'), ('Schedules', 'DbosSchedule', 'schedule'))
    return [
        {
            'model': None,
            'name': name,
            'object_name': object_name,
            'perms': {'add': False, 'change': False, 'delete': False, 'view': True},
            'admin_url': reverse(f'admin:django_dbos_{page}_changelist', current_app=site.name),
            'add_url': None,
            'view_only': True,
        }
        for name, object_name, page in pages
    ]


def register_admin(site: AdminSite) -> None:
    """Append django-dbos URLs to an AdminSite instance (idempotent)."""
    if site in _registered_sites:
        return

    if not site.is_registered(WorkerHeartbeat):
        site.register(WorkerHeartbeat, WorkerHeartbeatAdmin)

    original_get_urls = site.get_urls

    def dbos_view(view: admin_views.View) -> admin_views.View:
        return site.admin_view(admin_views.bind_site(view, site))

    def get_urls() -> list[URLPattern | URLResolver]:
        urls = original_get_urls()
        dbos_urls: list[URLPattern | URLResolver] = [
            path(
                f'{ADMIN_URL_PREFIX}workflows/',
                dbos_view(admin_views.workflow_list),
                name='django_dbos_workflow_changelist',
            ),
            path(
                f'{ADMIN_URL_PREFIX}workflows/<str:workflow_id>/',
                dbos_view(admin_views.workflow_detail),
                name='django_dbos_workflow_detail',
            ),
            path(
                f'{ADMIN_URL_PREFIX}workflows/<str:workflow_id>/cancel/',
                dbos_view(admin_views.workflow_cancel),
                name='django_dbos_workflow_cancel',
            ),
            path(
                f'{ADMIN_URL_PREFIX}workflows/<str:workflow_id>/resume/',
                dbos_view(admin_views.workflow_resume),
                name='django_dbos_workflow_resume',
            ),
            path(
                f'{ADMIN_URL_PREFIX}workflows/<str:workflow_id>/fork/',
                dbos_view(admin_views.workflow_fork),
                name='django_dbos_workflow_fork',
            ),
            path(
                f'{ADMIN_URL_PREFIX}workflows/<str:workflow_id>/delete/',
                dbos_view(admin_views.workflow_delete),
                name='django_dbos_workflow_delete',
            ),
            path(
                f'{ADMIN_URL_PREFIX}schedules/',
                dbos_view(admin_views.schedule_list),
                name='django_dbos_schedule_changelist',
            ),
            path(
                f'{ADMIN_URL_PREFIX}schedules/<str:schedule_name>/pause/',
                dbos_view(admin_views.schedule_pause),
                name='django_dbos_schedule_pause',
            ),
            path(
                f'{ADMIN_URL_PREFIX}schedules/<str:schedule_name>/resume/',
                dbos_view(admin_views.schedule_resume),
                name='django_dbos_schedule_resume',
            ),
            path(
                f'{ADMIN_URL_PREFIX}schedules/<str:schedule_name>/delete/',
                dbos_view(admin_views.schedule_delete),
                name='django_dbos_schedule_delete',
            ),
        ]
        # Prepend: the admin's own generic patterns (app_label/model_name)
        # would otherwise swallow paths like "dbos/workflows/".
        return dbos_urls + urls

    original_get_app_list = site.get_app_list

    def get_app_list(request: HttpRequest, app_label: str | None = None) -> list[dict[str, Any]]:
        app_list = original_get_app_list(request, app_label)
        if admin_views.is_active_superuser(request):
            for app in app_list:
                if app['app_label'] == APP_LABEL:
                    app['models'].extend(_dbos_page_entries(site))
                    app['models'].sort(key=lambda entry: entry['name'])
        return app_list

    site.get_urls = get_urls
    site.get_app_list = get_app_list
    _registered_sites.add(site)
    logger.debug('registered django-dbos admin views', admin_site=site.name)


def register_admin_sites() -> None:
    """Register admin views on every configured AdminSite (called on ready)."""
    if not is_admin_enabled():
        return
    for site in get_admin_sites():
        register_admin(site)
