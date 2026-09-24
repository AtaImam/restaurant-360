from django.urls import path
from . import views

urlpatterns = [
    # 1. KDS & Order Status
    path("", views.kitchen_dashboard, name="kitchen_dashboard"),
    path("orders/", views.kitchen_order_queue, name="kitchen_order_queue"),
    path("order/<int:order_id>/status/", views.update_order_status, name="update_order_status"),

    # 2. History & Performance
    path("history/", views.kitchen_order_history, name="kitchen_order_history"),
    path("performance/", views.kitchen_performance, name="kitchen_performance"),

    # 3. Menu Items & Availability
    path("menu-items/", views.kitchen_menu_items, name="kitchen_menu_items"),
    path("menu-items/<int:item_id>/toggle-availability/", views.toggle_item_availability, name="toggle_item_availability"),

    # 4. Recipes & Ingredients
    path("recipes/", views.kitchen_recipes, name="kitchen_recipes"),
    path("ingredients/", views.kitchen_ingredients, name="kitchen_ingredients"),
    path("low-stock/", views.kitchen_low_stock, name="kitchen_low_stock"),

    # 5. Ingredient Requests
    path("requests/", views.kitchen_ingredient_requests, name="kitchen_ingredient_requests"),
    path("requests/<int:request_id>/cancel/", views.kitchen_cancel_ingredient_request, name="kitchen_cancel_ingredient_request"),

    # 6. Staff Self-Service
    path("shift/", views.kitchen_shift, name="kitchen_shift"),
    path("attendance/", views.kitchen_attendance, name="kitchen_attendance"),
    path("attendance/check-in/", views.kitchen_check_in, name="kitchen_check_in"),
    path("attendance/check-out/", views.kitchen_check_out, name="kitchen_check_out"),
    path("leave/", views.kitchen_leave, name="kitchen_leave"),
    path("salary/", views.kitchen_salary, name="kitchen_salary"),
    path("salary-advance/", views.kitchen_salary_advance, name="kitchen_salary_advance"),
    path("payroll-history/", views.kitchen_payroll_history, name="kitchen_payroll_history"),

    # 7. Notifications
    path("notifications/<int:notification_id>/read/", views.kitchen_mark_notification_read, name="kitchen_mark_notification_read"),
    path("notifications/mark-all-read/", views.kitchen_mark_all_notifications_read, name="kitchen_mark_all_notifications_read"),
]