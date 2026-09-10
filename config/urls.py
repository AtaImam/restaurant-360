from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView


urlpatterns = [
    path("", RedirectView.as_view(url="auth/", permanent=False)),
    path("auth/", include("users.urls", namespace="users")),

    path("admin/", admin.site.urls),

    path("menu/", include("menu.urls")),
    path("kitchen/", include("kitchen.urls")),
    path("dashboard/", include("dashboard.urls")),

    path("inventory/", include("inventory.urls")),
    path("menu-management/", include("menu.management_urls")),
    path("pos/", include("pos.urls")),
]