from django.urls import path
from . import views

urlpatterns = [
    path("", views.pos_dashboard, name="pos_dashboard"),
    path("create-order/", views.create_pos_order, name="create_pos_order"),
    path("api/validate-coupon/", views.validate_pos_coupon, name="pos_validate_coupon"),
    path("table/<int:table_id>/close/", views.close_pos_table, name="close_pos_table"),
]