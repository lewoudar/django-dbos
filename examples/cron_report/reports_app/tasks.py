"""A durable, scheduled task: a signup report that fires every 10 seconds."""

from __future__ import annotations

import structlog
from django.tasks import task

from django_dbos import cron

from .models import Signup

logger = structlog.get_logger(__name__)


@cron('*/10 * * * * *')  # every 10 seconds (6-field crontab: leading seconds)
@task
def signup_report() -> None:
    total = Signup.objects.count()
    latest = list(Signup.objects.order_by('-created_at').values_list('email', flat=True)[:3])
    logger.info('signup report', total_signups=total, latest=latest)
