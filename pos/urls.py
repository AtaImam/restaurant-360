from django.urls import path
from . import views

urlpatterns = [
    path("", views.pos_dashboard, name="pos_dashboard"),
    path("create-order/", views.create_pos_order, name="create_pos_order"),
]