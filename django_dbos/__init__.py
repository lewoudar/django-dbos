"""DBOS-backed backend for Django's tasks framework."""

from importlib.metadata import version

from .context import get_dbos_context
from .params import dbos_params
from .scheduling import cron
from .tasks import DBOSTask

__version__ = version('django-dbos')

__all__ = ['DBOSTask', '__version__', 'cron', 'dbos_params', 'get_dbos_context']
