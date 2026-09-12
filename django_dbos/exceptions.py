"""django-dbos exceptions."""


class DjangoDbosError(Exception):
    """Base class for django-dbos errors."""


class ImproperlyConfiguredDbos(DjangoDbosError):
    """The DJANGO_DBOS setting or environment is misconfigured."""


class QueueNotDeclared(DjangoDbosError):
    """The task was enqueued on a queue that is not declared."""
