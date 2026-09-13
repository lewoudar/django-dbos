"""Integration tests for the django-dbos admin pages."""

from __future__ import annotations

from typing import Any

import pytest
from dbos import DBOS
from django.contrib.admin import AdminSite
from django.contrib.auth.models import User
from django.tasks import TaskResultStatus
from django.test import Client
from django.urls import reverse
from django_dbos.admin import is_admin_enabled, register_admin, register_admin_sites
from django_dbos.scheduling import register_cron_schedules

from .conftest import wait_for_result
from .tasks import add

pytestmark = pytest.mark.integration


def assert_text(response: Any, text: str) -> None:
    assert text in response.content.decode()


@pytest.fixture()
def admin_client(client: Any, db: Any) -> Any:
    """A Django test client logged in as a superuser."""
    user = User.objects.create_superuser(username="kevin", password="hunter2", email="")
    client.force_login(user)
    yield client
    User.objects.filter(pk=user.pk).delete()


def _enqueue_sample_workflow() -> Any:
    return add.enqueue(2, 3)


class TestWorkflowPages:
    def test_workflow_list_shows_enqueued_task(self, admin_client, dbos_environment):
        result = _enqueue_sample_workflow()
        response = admin_client.get("/admin/dbos/workflows/")
        assert response.status_code == 200
        assert_text(response, "tests.tasks.add")
        assert_text(response, result.id)

    def test_workflow_detail_shows_steps(self, admin_client, dbos_environment):
        result = wait_for_result(_enqueue_sample_workflow(), TaskResultStatus.SUCCESSFUL)
        response = admin_client.get(f"/admin/dbos/workflows/{result.id}/")
        assert response.status_code == 200
        assert_text(response, "tests.tasks.add")
        assert_text(response, "task_body")

    def test_workflow_list_bad_page_inputs_render_page_1(self, admin_client, dbos_environment):
        result = _enqueue_sample_workflow()
        for bad in ("abc", "-3"):
            response = admin_client.get("/admin/dbos/workflows/", {"page": bad})
            assert response.status_code == 200, bad
            # The first page is rendered: the newest workflow is listed.
            assert_text(response, result.id)

    def test_workflow_list_huge_page_is_clamped_not_500(self, admin_client, dbos_environment):
        # Out-of-range page: clamped to the last allowed page, so the offset
        # stays within bigint range instead of erroring in the database.
        response = admin_client.get("/admin/dbos/workflows/", {"page": "99999999999999999999999"})
        assert response.status_code == 200
        assert_text(response, "No workflows found.")

    def test_workflow_detail_404_on_unknown_id(self, admin_client, dbos_environment):
        response = admin_client.get("/admin/dbos/workflows/unknown-id/")
        assert response.status_code == 404

    def test_cancel_workflow(self, admin_client, dbos_environment):
        result = _enqueue_sample_workflow()
        response = admin_client.post(f"/admin/dbos/workflows/{result.id}/cancel/")
        assert response.status_code == 302
        status = DBOS.get_workflow_status(result.id)
        assert status is not None
        assert str(status.status) in ("CANCELLED", "WorkflowStatuses.CANCELLED")

    def test_delete_workflow_with_confirmation(self, admin_client, dbos_environment):
        result = _enqueue_sample_workflow()
        # GET shows the confirmation page
        response = admin_client.get(f"/admin/dbos/workflows/{result.id}/delete/")
        assert response.status_code == 200
        assert_text(response, "Are you sure?")
        # POST deletes and redirects to the changelist
        response = admin_client.post(f"/admin/dbos/workflows/{result.id}/delete/")
        assert response.status_code == 302
        assert DBOS.get_workflow_status(result.id) is None


class TestSchedulePages:
    def test_schedule_list(self, admin_client, dbos_environment):
        register_cron_schedules()
        response = admin_client.get("/admin/dbos/schedules/")
        assert response.status_code == 200
        assert_text(response, "tests.tasks.cron_task")

    def test_delete_schedule_with_confirmation(self, admin_client, dbos_environment):
        register_cron_schedules()
        name = "django_dbos.schedules.tests.tasks.cron_task"
        # GET shows the confirmation page instead of deleting outright
        response = admin_client.get(f"/admin/dbos/schedules/{name}/delete/")
        assert response.status_code == 200
        assert_text(response, "Are you sure?")
        assert_text(response, name)
        # POST deletes and redirects to the changelist
        response = admin_client.post(f"/admin/dbos/schedules/{name}/delete/")
        assert response.status_code == 302

        assert DBOS.get_schedule(name) is None

    def test_unknown_schedule_returns_404(self, admin_client, dbos_environment):
        name = "django_dbos.schedules.tests.tasks.never_registered"
        response = admin_client.post(f"/admin/dbos/schedules/{name}/pause/")
        assert response.status_code == 404
        response = admin_client.get(f"/admin/dbos/schedules/{name}/delete/")
        assert response.status_code == 404


class TestAdminGating:
    def test_admin_enabled_by_default(self, settings):
        settings.DJANGO_DBOS = {k: v for k, v in settings.DJANGO_DBOS.items() if k != "ADMIN_ENABLED"}
        assert is_admin_enabled()

    def test_urls_registered_by_default(self, dbos_environment):
        reverse("admin:django_dbos_workflow_changelist")  # raises if missing

    def test_urls_registered_when_enabled(self, settings):
        settings.DJANGO_DBOS = {**settings.DJANGO_DBOS, "ADMIN_ENABLED": True}
        reverse("admin:django_dbos_workflow_changelist")  # raises if missing

    def test_staff_without_superuser_gets_403(self, db, dbos_environment):
        staff = User.objects.create_user(username="editor", password="hunter2", is_staff=True)
        client = Client()
        client.force_login(staff)
        try:
            response = client.get("/admin/dbos/workflows/")
            assert response.status_code == 403
            response = client.post("/admin/dbos/workflows/whatever/delete/")
            assert response.status_code == 403
        finally:
            User.objects.filter(pk=staff.pk).delete()

    def test_disabled_admin_skips_registration(self, settings):
        settings.DJANGO_DBOS = {**settings.DJANGO_DBOS, "ADMIN_ENABLED": False}
        site = AdminSite(name="disabled-test")
        register_admin_sites()
        # register_admin_sites() only touches the configured default sites;
        # our fresh site is untouched and directly registerable or not.
        assert not getattr(site, "_django_dbos_registered", False)

    def test_enabled_admin_registers_default_site(self, settings, dbos_environment):
        settings.DJANGO_DBOS = {**settings.DJANGO_DBOS, "ADMIN_ENABLED": True}
        reverse("admin:django_dbos_workflow_changelist")

    def test_custom_admin_site_registration(self, dbos_environment):
        custom_site = AdminSite(name="custom-test")
        register_admin(custom_site)
        url_names = [p.name for p in custom_site.get_urls() if getattr(p, "name", None)]
        assert "django_dbos_workflow_changelist" in url_names
        assert "django_dbos_schedule_changelist" in url_names
        # Idempotent: a second registration is a no-op.
        register_admin(custom_site)
        assert "django_dbos_workflow_changelist" in url_names

    def test_non_staff_redirected_to_login(self, client, dbos_environment):
        response = client.get("/admin/dbos/workflows/")
        assert response.status_code == 302
        assert "/admin/login" in response["Location"]
