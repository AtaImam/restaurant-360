from django.urls import path

from orders import coupon_views
from . import management_views


urlpatterns = [
    path(
        "items/",
        management_views.menu_item_list,
        name="menu_item_list",
    ),

    path(
        "items/add/",
        management_views.menu_item_create,
        name="menu_item_create",
    ),

    path(
        "items/<int:item_id>/recipe/",
        management_views.recipe_builder,
        name="recipe_builder",
    ),
    path(
        "items/<int:item_id>/edit/",
        management_views.menu_item_edit,
        name="menu_item_edit",
    ),
    path(
        "items/<int:item_id>/toggle-availability/",
        management_views.menu_item_toggle_availability,
        name="menu_item_toggle_availability",
    ),
    path(
        "items/<int:item_id>/delete/",
        management_views.menu_item_delete,
        name="menu_item_delete",
    ),
    path(
        "addons/",
        management_views.addon_group_list,
        name="addon_group_list",
    ),
    path(
        "addons/add/",
        management_views.addon_group_create,
        name="addon_group_create",
    ),
    path(
        "addons/<int:group_id>/edit/",
        management_views.addon_group_edit,
        name="addon_group_edit",
    ),
    path(
        "addons/<int:group_id>/delete/",
        management_views.addon_group_delete,
        name="addon_group_delete",
    ),
    path(
        "coupons/",
        coupon_views.coupon_list,
        name="coupon_list",
    ),
    path(
        "coupons/add/",
        coupon_views.coupon_create,
        name="coupon_create",
    ),
    path(
        "coupons/<int:coupon_id>/edit/",
        coupon_views.coupon_edit,
        name="coupon_edit",
    ),
    path(
        "coupons/<int:coupon_id>/toggle/",
        coupon_views.coupon_toggle,
        name="coupon_toggle",
    ),
    path(
        "coupons/<int:coupon_id>/delete/",
        coupon_views.coupon_delete,
        name="coupon_delete",
    ),
]