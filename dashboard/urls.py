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
]