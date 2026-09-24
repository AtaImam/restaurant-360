from django.urls import path

from . import views


urlpatterns = [
    path(
        "",
        views.owner_dashboard,
        name="owner_dashboard",
    ),
    path(
        "api/analytics/",
        views.dashboard_analytics_api,
        name="dashboard_analytics_api",
    ),

    path(
        "orders/",
        views.orders_list,
        name="orders_list",
    ),

    path(
        "orders/stale/",
        views.stale_orders_list,
        name="stale_orders_list",
    ),
    path(
        "orders/stale/archive-all/",
        views.archive_all_stale_orders_view,
        name="archive_all_stale_orders",
    ),
    path(
        "orders/<int:order_id>/archive/",
        views.archive_stale_order_view,
        name="archive_stale_order",
    ),
    path(
        "orders/<int:order_id>/",
        views.order_detail,
        name="order_detail",
    ),

    path(
        "categories/",
        views.category_list,
        name="category_list",
    ),

    path(
        "categories/add/",
        views.category_create,
        name="category_create",
    ),

    path(
        "categories/<int:category_id>/edit/",
        views.category_edit,
        name="category_edit",
    ),

    path(
        "categories/<int:category_id>/delete/",
        views.category_delete,
        name="category_delete",
    ),
]