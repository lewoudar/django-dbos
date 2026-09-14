"""Tests for task workflow registration and name resolution."""

import pytest

from django_dbos.tasks_registry import task_from_workflow_name

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


class TaskFromWorkflowNameTest:
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
