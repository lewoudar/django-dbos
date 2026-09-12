"""A cron task with required arguments: importing this module must fail."""

from django.tasks import task

from django_dbos import cron


@cron("*/5 * * * *")
@task(backend="dbos")
def cron_task_with_args(user_id: int) -> int:
    return user_id
