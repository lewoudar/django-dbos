"""The DBOS workflow name of a Django task, and back."""

from __future__ import annotations

import re
from typing import Any

from django.utils.module_loading import import_string

from .tasks import DBOSTask

REGISTRY_PREFIX = 'django_dbos.tasks.'

# Task module paths resolved via ``import_string`` come from a name stored in
# the system database (workflow status); reject anything that is not a plain
# dotted module path before importing it.
_MODULE_PATH_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$')


def workflow_name_for_task(task: DBOSTask) -> str:
    """The DBOS workflow name for a task: its Django module path."""
    return f'{REGISTRY_PREFIX}{task.module_path}'


def task_from_workflow_name(workflow_name: str) -> Any:
    """Resolve a registered workflow name back to its DBOSTask instance."""
    if not workflow_name.startswith(REGISTRY_PREFIX):
        return None

    module_path = workflow_name.removeprefix(REGISTRY_PREFIX)
    if not _MODULE_PATH_RE.fullmatch(module_path):
        return None

    try:
        task = import_string(module_path)
    except ImportError:  # import_string also reports a missing attribute as ImportError
        return None
    # Only actual tasks are usable as results; the name comes from the system
    # database, so anything else (arbitrary module attributes, ...) is rejected.
    return task if isinstance(task, DBOSTask) else None
