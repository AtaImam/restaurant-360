from django.urls import path
from . import views

urlpatterns = [
    path(
        '<int:restaurant_id>/table/<int:table_id>/',
        views.customer_menu,
        name='customer_menu'
    ),

    path(
        '<int:restaurant_id>/table/<int:table_id>/add/<int:item_id>/',
        views.add_to_cart,
        name='add_to_cart'
    ),
    path(
    '<int:restaurant_id>/table/<int:table_id>/cart/',
    views.cart_view,
    name='cart'
),
path(
    '<int:restaurant_id>/table/<int:table_id>/cart/increase/<int:item_id>/',
    views.increase_cart_item,
    name='increase_cart_item'
),

path(
    '<int:restaurant_id>/table/<int:table_id>/cart/decrease/<int:item_id>/',
    views.decrease_cart_item,
    name='decrease_cart_item'
),

path(
    '<int:restaurant_id>/table/<int:table_id>/cart/remove/<int:item_id>/',
    views.remove_cart_item,
    name='remove_cart_item'
),
path(
    '<int:restaurant_id>/table/<int:table_id>/checkout/',
    views.checkout,
    name='checkout'
),

path(
    'order/<int:order_id>/success/',
    views.order_success,
    name='order_success'
),
path(
    '<int:restaurant_id>/table/<int:table_id>/checkout/',
    views.checkout,
    name='checkout'
),

path(
    'order/<int:order_id>/success/',
    views.order_success,
    name='order_success'
),
]