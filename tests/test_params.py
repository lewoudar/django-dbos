"""Unit tests for the dbos_params decorator."""

import dataclasses

import pytest
from django.tasks import task as django_task
from django.tasks.exceptions import InvalidTask

from django_dbos import dbos_params
from django_dbos.exceptions import ImproperlyConfiguredDbos
from django_dbos.params import resolve_deduplication_id, resolve_partition_key
from django_dbos.params_dataclass import FUNC_PARAMS_ATTRIBUTE, DbosTaskParams

from .helpers import make_dbos_task
from .tasks import add, context_task, greet_user


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

    def test_on_dbos_task_instance_returns_replaced_task(self):
        task = make_dbos_task()
        decorated = dbos_params(max_recovery_attempts=7)(task)
        assert decorated is not task
        assert isinstance(decorated, type(task))
        assert decorated.dbos_params.max_recovery_attempts == 7
        # The original task is untouched (frozen dataclass replacement).
        assert task.dbos_params.max_recovery_attempts is None

    def test_defaults_match_the_dataclass(self):
        # DBOSTask.__post_init__ reads DbosTaskParams() as "no params given".
        dbos_params()(bare_function)
        assert getattr(bare_function, FUNC_PARAMS_ATTRIBUTE) == DbosTaskParams()

    def test_on_task_from_another_backend_keeps_function_attribute(self):
        decorated = dbos_params(workflow_timeout=30)(foreign_task)
        assert decorated is foreign_task
        assert foreign_task.func._django_dbos_params.workflow_timeout == 30

    def test_bare_function_round_trip(self):
        dbos_params(step_max_attempts=5)(bare_function)
        params: DbosTaskParams = getattr(bare_function, FUNC_PARAMS_ATTRIBUTE)
        assert params.step_max_attempts == 5

    def test_partition_key_must_be_a_name_or_a_callable(self):
        with pytest.raises(ImproperlyConfiguredDbos, match=r'^partition_key must be the name of a task argument'):
            dbos_params(partition_key=42)

    def test_deduplication_id_must_be_a_string_or_a_callable(self):
        with pytest.raises(ImproperlyConfiguredDbos, match=r'^deduplication_id must be a string or a callable'):
            dbos_params(deduplication_id=42)

    def test_deduplication_id_must_not_be_empty(self):
        with pytest.raises(ImproperlyConfiguredDbos, match=r'^deduplication_id must not be empty\.$'):
            dbos_params(deduplication_id='')

    def test_partition_key_below_task_is_validated_with_the_task(self):
        # @dbos_params under @task: the params are picked up before the backend validates the task.
        dbos_params(partition_key='nope')(function_with_a_bad_partition_key)
        with pytest.raises(InvalidTask, match="partition_key 'nope' is not an argument"):
            dataclasses.replace(context_task, func=function_with_a_bad_partition_key)


def function_with_a_bad_partition_key(context: object, user_id: int) -> None: ...


def function_with_context(context: object, user_id: int) -> None: ...


@pytest.mark.usefixtures('keep_known_tasks')
class TestResolvePartitionKey:
    def test_no_partition_key(self):
        assert resolve_partition_key(add, (1, 2), {}) is None

    def test_an_argument_left_to_its_default(self):
        task = dbos_params(partition_key='greeting')(greet_user)
        assert resolve_partition_key(task, (1,), {}) == 'hello'

    def test_a_callable_receives_the_arguments(self):
        task = dbos_params(partition_key=lambda user_id, greeting='': f'{greeting}-{user_id}')(greet_user)
        assert resolve_partition_key(task, (1,), {'greeting': 'hi'}) == 'hi-1'

    def test_the_context_parameter_is_skipped(self):
        task = dataclasses.replace(
            context_task, func=function_with_context, dbos_params=DbosTaskParams(partition_key='user_id')
        )
        assert resolve_partition_key(task, (5,), {}) == '5'

    @pytest.mark.parametrize('value', [None, ''])
    def test_an_empty_key_is_refused(self, value):
        task = dbos_params(partition_key=lambda *args, **kwargs: value)(greet_user)
        with pytest.raises(ImproperlyConfiguredDbos, match=r"^The partition key of task 'greet_user' is empty"):
            resolve_partition_key(task, (1,), {})

    def test_arguments_not_matching_the_signature_are_refused(self):
        with pytest.raises(ImproperlyConfiguredDbos, match=r"^Cannot read the partition key of task 'greet_user'"):
            resolve_partition_key(greet_user, (), {'unknown': 1})


@pytest.mark.usefixtures('keep_known_tasks')
class TestResolveDeduplicationId:
    def test_no_deduplication_id(self):
        assert resolve_deduplication_id(add, (1, 2), {}) is None

    def test_a_fixed_id_is_kept_as_is(self):
        task = dbos_params(deduplication_id='nightly-cleanup')(add)
        assert resolve_deduplication_id(task, (1, 2), {}) == 'nightly-cleanup'

    def test_a_callable_receives_the_arguments(self):
        task = dbos_params(deduplication_id=lambda user_id, greeting='': f'{greeting}-{user_id}')(greet_user)
        assert resolve_deduplication_id(task, (1,), {'greeting': 'hi'}) == 'hi-1'

    def test_a_non_string_value_is_converted(self):
        task = dbos_params(deduplication_id=lambda user_id, **kwargs: user_id)(greet_user)
        assert resolve_deduplication_id(task, (42,), {}) == '42'

    def test_none_skips_deduplication_for_this_enqueue(self):
        task = dbos_params(deduplication_id=lambda *args, **kwargs: None)(greet_user)
        assert resolve_deduplication_id(task, (1,), {}) is None

    def test_an_empty_id_is_refused(self):
        task = dbos_params(deduplication_id=lambda *args, **kwargs: '')(greet_user)
        with pytest.raises(ImproperlyConfiguredDbos, match=r"^The deduplication ID of task 'greet_user' is empty"):
            resolve_deduplication_id(task, (1,), {})
