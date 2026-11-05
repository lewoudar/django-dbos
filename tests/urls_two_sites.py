"""URLconf with a second admin site next to the default one, for multi-site tests."""

from django.contrib import admin
from django.contrib.admin import AdminSite
from django.urls import path

from django_dbos.admin import register_admin

ops_site = AdminSite(name='ops')
register_admin(ops_site)

urlpatterns = [
    path('admin/', admin.site.urls),
    path('ops/', ops_site.urls),
]
