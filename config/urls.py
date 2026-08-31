from django.contrib import admin
from django.urls import include, path


urlpatterns = [
    path(
        "admin/",
        admin.site.urls,
    ),

    path(
        "menu/",
        include("menu.urls"),
    ),

    path(
        "kitchen/",
        include("kitchen.urls"),
    ),

    path(
        "dashboard/",
        include("dashboard.urls"),
    ),

    path(
        "inventory/",
        include("inventory.urls"),
    ),

    path(
        "menu-management/",
        include("menu.management_urls"),
    ),
]