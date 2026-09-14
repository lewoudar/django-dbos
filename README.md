# django-dbos

DBOS-backed backend for Django's tasks framework (`django.tasks`): durable
background tasks, cron scheduling and optional Sentry monitoring.

```python
from django.tasks import task


@task
def send_welcome_email(user_id: int): ...


result = send_welcome_email.enqueue(user_id)  # durable, survives crashes
```

Documentation: https://github.com/lewoudar/django-dbos (full docs coming soon).

## License

MIT
