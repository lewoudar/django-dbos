"""Tests for task workflow registration and name resolution."""

import pytest

from django_dbos.tasks import known_tasks
from django_dbos.tasks_registry import register_all_task_workflows
from django_dbos.workflow_names import task_from_workflow_name, workflow_name_for_task

from . import tasks as tasks_module

MALICIOUS_WORKFLOW_NAMES = [
    # Attribute traversal towards arbitrary modules.
    pytest.param('django_dbos.tasks.logging.sys.modules', id='attribute-traversal'),
    pytest.param('django_dbos.tasks.tests.tasks.__init__', id='dunder-segment'),
    pytest.param('django_dbos.tasks..tests.tasks.add', id='empty-segment'),
    pytest.param('django_dbos.tasks.', id='trailing-empty-segment'),
    pytest.param('django_dbos.tasks.tests tasks.add', id='space-in-name'),
    pytest.param('django_dbos.tasks.tests.tasks.not_a_task', id='not-a-task'),
    pytest.param('django_dbos.tasks.not_a_module.thing', id='module-does-not-exist'),
]


class TestTaskFromWorkflowName:
    def test_resolves_valid_task(self):
        task = task_from_workflow_name('django_dbos.tasks.tests.tasks.add')
        assert task is not None
        assert task.module_path == 'tests.tasks.add'

    def test_non_prefixed_name_is_ignored(self):
        assert task_from_workflow_name('some_other_workflow') is None

    @pytest.mark.parametrize('name', MALICIOUS_WORKFLOW_NAMES)
    def test_malicious_name_is_rejected(self, name):
        # The name comes from the system database: it must never resolve to
        # anything outside the registered task workflows.
        assert task_from_workflow_name(name) is None


class TestKnownTasks:
    def test_declared_tasks_are_known(self):
        # @task evaluates at import: every task of tests.tasks is known once imported.
        module_paths = {task.module_path for task in known_tasks()}
        assert {'tests.tasks.add', 'tests.tasks.send_email', 'tests.tasks.durable_onboarding'} <= module_paths

    def test_last_declaration_wins(self):
        # @dbos_params above @task copies the instance built by @task: the copy replaces it in the registry.
        (registered,) = [task for task in known_tasks() if task.module_path == 'tests.tasks.slow_task']
        assert registered is tasks_module.slow_task
        assert registered.dbos_params.workflow_timeout == 1

    def test_using_does_not_shadow_the_declared_task(self):
        tasks_module.add.using(priority=10)  # transient copy, same function and params
        (registered,) = [task for task in known_tasks() if task.module_path == 'tests.tasks.add']
        assert registered.func is tasks_module.add.func
        assert registered.dbos_params == tasks_module.add.dbos_params


class TestRegisterAllTaskWorkflows:
    def test_registers_every_known_task(self, mocker):
        seen: list = []
        mocker.patch('django_dbos.tasks_registry.register_task_workflow', side_effect=seen.append)

        register_all_task_workflows()

        assert {workflow_name_for_task(task) for task in seen} >= {
            'django_dbos.tasks.tests.tasks.add',
            'django_dbos.tasks.tests.tasks.async_add',
        }
