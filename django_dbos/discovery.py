"""Autodiscovery of task modules across installed apps."""

from __future__ import annotations

import importlib

from django.apps import apps
from django.utils.module_loading import module_has_submodule

from .logging import logger

TASK_MODULE_NAMES = ('tasks', 'dbos_tasks')


def autodiscover_tasks() -> None:
    """Import each installed app's ``tasks``/``dbos_tasks`` module.

    Importing the module evaluates its ``@task`` decorators, which is required
    before enqueueing (workflows are registered lazily) and lets the worker
    and the cron scheduler see every declared task.
    """
    for app_config in apps.get_app_configs():
        for module_name in TASK_MODULE_NAMES:
            module = f'{app_config.name}.{module_name}'
            try:
                importlib.import_module(module)
            except ImportError:
                if module_has_submodule(app_config.module, module_name):
                    raise
                continue
        logger.debug('autodiscovered tasks', app=app_config.name)
