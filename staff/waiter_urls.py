from django.urls import path
from . import waiter_views

app_name = "waiter"

urlpatterns = [
    # Dashboard
    path("", waiter_views.waiter_dashboard, name="dashboard"),

    # Operations
    path("tables/", waiter_views.waiter_tables, name="tables"),
    path("tables/<int:table_id>/clean/", waiter_views.waiter_clean_table_action, name="clean_table"),
    path("ready/", waiter_views.waiter_ready_orders, name="ready_orders"),
    path("serve/<int:order_id>/", waiter_views.waiter_serve_order_action, name="serve_order"),
    path("claim/<int:order_id>/", waiter_views.waiter_claim_order_action, name="claim_order"),
    path("tasks/", waiter_views.waiter_tasks, name="tasks"),
    path("tasks/<int:task_id>/complete/", waiter_views.waiter_complete_task, name="complete_task"),

    # My Work
    path("shift/", waiter_views.waiter_shift, name="shift"),
    path("attendance/", waiter_views.waiter_attendance, name="attendance"),
    path("attendance/check-in/", waiter_views.waiter_check_in, name="check_in"),
    path("attendance/check-out/", waiter_views.waiter_check_out, name="check_out"),
    path("leave/", waiter_views.waiter_leave, name="leave"),
    path("history/", waiter_views.waiter_service_history, name="service_history"),
    path("performance/", waiter_views.waiter_performance, name="performance"),

    # Payroll
    path("salary/", waiter_views.waiter_salary, name="salary"),
    path("salary-advance/", waiter_views.waiter_salary_advance, name="salary_advance"),
    path("payroll-history/", waiter_views.waiter_payroll_history, name="payroll_history"),

    # Notifications
    path("notifications/<int:notification_id>/read/", waiter_views.waiter_mark_notification_read, name="notification_read"),
    path("notifications/mark-all-read/", waiter_views.waiter_mark_all_notifications_read, name="mark_all_notifications_read"),
]
