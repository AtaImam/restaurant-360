from django.urls import path
from . import views

urlpatterns = [
    path('', views.owner_dashboard, name='owner_dashboard'),

    path(
        'orders/',
        views.orders_list,
        name='orders_list'
    ),
     path(
        'orders/<int:order_id>/',
        views.order_detail,
        name='order_detail'
    ),
    path(
    'categories/',
    views.category_list,
    name='category_list'
),

path(
    'categories/add/',
    views.category_create,
    name='category_create'
),

path(
    'categories/<int:category_id>/edit/',
    views.category_edit,
    name='category_edit'
),

path(
    'categories/<int:category_id>/delete/',
    views.category_delete,
    name='category_delete'
),
]