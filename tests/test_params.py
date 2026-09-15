"""Unit tests for the dbos_params decorator."""

from django.tasks import task as django_task

from django_dbos import dbos_params
from django_dbos.params import FUNC_PARAMS_ATTRIBUTE
from django_dbos.params_dataclass import DbosTaskParams

from .tests_helpers import make_dbos_task


def bare_function() -> None: ...


@django_task
def foreign_task() -> None: ...


class TestDbosParams:
    def test_bare_function_gets_params_attribute(self):
        decorated = dbos_params(durable_operations=True)(bare_function)
        assert decorated is bare_function
        assert bare_function._django_dbos_params.durable_operations is True

    def test_used_without_parentheses(self):
        @dbos_params
        def some_task() -> None: ...

        assert some_task._django_dbos_params is not None

    def test_on_dbostask_instance_returns_replaced_task(self):
        task = make_dbos_task()
        decorated = dbos_params(max_recovery_attempts=7)(task)
        assert decorated is not task
        assert isinstance(decorated, type(task))
        assert decorated.dbos_params.max_recovery_attempts == 7
        # The original task is untouched (frozen dataclass replacement).
        assert task.dbos_params.max_recovery_attempts is None

    def test_defaults(self):
        params = DbosTaskParams()
        assert params.step_max_attempts == 3
        assert params.step_retries_allowed is False
        assert params.durable_operations is False

    def test_on_task_from_another_backend_keeps_function_attribute(self):
        decorated = dbos_params(workflow_timeout=30)(foreign_task)
        assert decorated is foreign_task
        assert foreign_task.func._django_dbos_params.workflow_timeout == 30

    def test_bare_function_round_trip(self):
        dbos_params(step_max_attempts=5)(bare_function)
        params: DbosTaskParams = getattr(bare_function, FUNC_PARAMS_ATTRIBUTE)
        assert params.step_max_attempts == 5
