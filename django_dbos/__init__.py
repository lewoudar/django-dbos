"""DBOS-backed backend for Django's tasks framework."""

from importlib.metadata import version

from .context import as_completed, get_dbos_context
from .params import dbos_params
from .retention import purge_workflows
from .scheduling import cron
from .tasks import DBOSTask

__version__ = version('django-dbos')

__all__ = ['DBOSTask', '__version__', 'as_completed', 'cron', 'dbos_params', 'get_dbos_context', 'purge_workflows']
