"""Autodiscovery of task modules across installed apps."""

from __future__ import annotations

import importlib

from django.apps import apps
from django.utils.module_loading import module_has_submodule

from .logging import logger

TASK_MODULE_NAMES = ('tasks', 'dbos_tasks')


def autodiscover_tasks() -> list[str]:
    """Import each installed app's ``tasks``/``dbos_tasks`` module.

    Importing the module evaluates its ``@task`` and ``@cron`` decorators,
    which fill the task and cron registries: the worker registers them as
    DBOS workflows before launch, and the system checks inspect them.
    Clients do not need it, they enqueue by workflow name.

    Returns:
        The names of the imported modules.

    Raises:
        ImportError: A task module exists but fails to import.
    """
    imported: list[str] = []
    for app_config in apps.get_app_configs():
        for module_name in TASK_MODULE_NAMES:
            module = f'{app_config.name}.{module_name}'
            try:
                importlib.import_module(module)
            except ImportError:
                if module_has_submodule(app_config.module, module_name):
                    raise
                continue
            imported.append(module)
        logger.debug('autodiscovered tasks', app=app_config.name)
    return imported
