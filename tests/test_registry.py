"""Tests for task workflow registration and name resolution."""

from django.test import SimpleTestCase

from django_dbos.tasks_registry import task_from_workflow_name


class TaskFromWorkflowNameTest(SimpleTestCase):
    def test_resolves_valid_task(self):
        task = task_from_workflow_name("django_dbos.tasks.tests.tasks.add")
        assert task is not None
        assert task.module_path == "tests.tasks.add"

    def test_non_prefixed_name_is_ignored(self):
        assert task_from_workflow_name("some_other_workflow") is None

    def test_malicious_path_is_rejected(self):
        # Attribute traversal, dunder segments and empty segments must not
        # resolve to anything, since the name comes from the system database.
        for name in (
            "django_dbos.tasks.logging.sys.modules",
            "django_dbos.tasks.tests.tasks.__init__",
            "django_dbos.tasks..tests.tasks.add",
            "django_dbos.tasks.",
            "django_dbos.tasks.tests tasks.add",
            "django_dbos.tasks.tests.tasks.not_a_task",
        ):
            assert task_from_workflow_name(name) is None, name
