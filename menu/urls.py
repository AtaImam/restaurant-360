from django.urls import path

from . import views
from .tracking_views import order_status_api


urlpatterns = [

    # ========================================================
    # CUSTOMER MENU
    # ========================================================

    path(
        "<int:restaurant_id>/table/<int:table_id>/",
        views.customer_menu,
        name="customer_menu",
    ),

    # ========================================================
    # FOOD DETAILS
    # ========================================================

    path(
        "<int:restaurant_id>/table/<int:table_id>/item/<int:item_id>/",
        views.item_detail,
        name="item_detail",
    ),

    # ========================================================
    # ADD TO CART
    # ========================================================

    path(
        "<int:restaurant_id>/table/<int:table_id>/add/<int:item_id>/",
        views.add_to_cart,
        name="add_to_cart",
    ),

    # ========================================================
    # CART
    # ========================================================

    path(
        "<int:restaurant_id>/table/<int:table_id>/cart/",
        views.cart_view,
        name="cart",
    ),

    path(
        "<int:restaurant_id>/table/<int:table_id>/cart/increase/<str:item_id>/",
        views.increase_cart_item,
        name="increase_cart_item",
    ),

    path(
        "<int:restaurant_id>/table/<int:table_id>/cart/decrease/<str:item_id>/",
        views.decrease_cart_item,
        name="decrease_cart_item",
    ),

    path(
        "<int:restaurant_id>/table/<int:table_id>/cart/remove/<str:item_id>/",
        views.remove_cart_item,
        name="remove_cart_item",
    ),

    # ========================================================
    # CHECKOUT
    # ========================================================

    path(
        "<int:restaurant_id>/table/<int:table_id>/checkout/",
        views.checkout,
        name="checkout",
    ),

    # ========================================================
    # CUSTOMER ORDER TRACKING
    # ========================================================

    path(
        "order/<int:order_id>/success/",
        views.order_success,
        name="order_success",
    ),

    path(
        "order/<int:order_id>/status/",
        order_status_api,
        name="order_status_api",
    ),
]