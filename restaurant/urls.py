from django.urls import path

from . import management_views


app_name = "restaurant"


urlpatterns = [
    path(
        "floors/",
        management_views.floor_management,
        name="floor_management",
    ),
    path(
        "floors/<int:floor_id>/update/",
        management_views.floor_update,
        name="floor_update",
    ),
    path(
        "floors/<int:floor_id>/toggle/",
        management_views.floor_toggle,
        name="floor_toggle",
    ),
    path(
        "tables/",
        management_views.table_management,
        name="table_management",
    ),
    path(
        "tables/<int:table_id>/update/",
        management_views.table_update,
        name="table_update",
    ),
    path(
        "tables/<int:table_id>/toggle/",
        management_views.table_toggle,
        name="table_toggle",
    ),
    path(
        "tables/<int:table_id>/qr/download/",
        management_views.qr_download,
        name="qr_download",
    ),
    path(
        "tables/<int:table_id>/qr/print/",
        management_views.qr_print,
        name="qr_print",
    ),
]