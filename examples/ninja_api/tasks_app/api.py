"""Submit a task and poll its result through a small Django Ninja API."""

from __future__ import annotations

from django.tasks import TaskResultStatus
from ninja import NinjaAPI, Schema

from .tasks import count_words

api = NinjaAPI(title='django-dbos ninja example')


class WordCountIn(Schema):
    text: str


class TaskSubmitted(Schema):
    task_id: str
    status: str


class TaskStatusOut(Schema):
    task_id: str
    status: str
    result: dict[str, int] | None = None


@api.post('/tasks/word-count', response=TaskSubmitted)
def submit_word_count(request, payload: WordCountIn):
    """Enqueue count_words and return its task id right away."""
    result = count_words.enqueue(payload.text)
    return {'task_id': result.id, 'status': result.status}


@api.get('/tasks/{task_id}', response=TaskStatusOut)
def get_task_status(request, task_id: str):
    """Poll a previously submitted task by id."""
    result = count_words.get_result(task_id)
    return {
        'task_id': result.id,
        'status': result.status,
        'result': result.return_value if result.status == TaskResultStatus.SUCCESSFUL else None,
    }
