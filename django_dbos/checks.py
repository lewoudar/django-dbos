"""System checks for django-dbos."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.checks import Error, Info, register

from .exceptions import ImproperlyConfiguredDbos


@register()
def check_dbos_settings(app_configs: Any = None, **kwargs: Any) -> list[Error | Info]:
    from . import settings as settings_module

    errors: list[Error | Info] = []
    try:
        settings_module.resolve_settings()
    except ImproperlyConfiguredDbos as exc:
        errors.append(
            Error(
                str(exc),
                id="django_dbos.E001",
                hint=f"Fix your {settings_module.SETTING_NAME} setting or "
                "DBOS_SYSTEM_DATABASE_URL environment variable.",
            )
        )
    return errors


@register()
def check_system_database_engine(app_configs: Any = None, **kwargs: Any) -> list[Error | Info]:
    from . import settings as settings_module

    default_engine = (settings.DATABASES.get("default", {}) or {}).get("ENGINE", "")
    explicit_url = settings_module.get_dbos_setting("SYSTEM_DATABASE_URL")
    if explicit_url or "postgres" in default_engine or "sqlite" in default_engine:
        return []

    return [
        Info(
            "The default Django database engine does not allow django-dbos to derive a DBOS system database URL.",
            id="django_dbos.I001",
            hint=f"Set {settings_module.SETTING_NAME}['SYSTEM_DATABASE_URL'] or DBOS_SYSTEM_DATABASE_URL.",
        )
    ]
