from django.urls import path
from . import views

urlpatterns = [
    path('', views.kitchen_dashboard, name='kitchen_dashboard'),

    path(
        'order/<int:order_id>/status/',
        views.update_order_status,
        name='update_order_status'
    ),
]