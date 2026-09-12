"""structlog helpers for django-dbos.

django-dbos logs through plain ``structlog`` loggers so its records blend with
whatever logging configuration the project already uses (stdlib logging,
``django-structlog``, ...). No global configuration is imposed; we only
document a recommended setup in the docs.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger("django_dbos")
