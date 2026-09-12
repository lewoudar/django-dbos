"""URLconf for the test suite: exposes the default admin site."""

from django.contrib import admin
from django.urls import path

urlpatterns = [
    path("admin/", admin.site.urls),
]
