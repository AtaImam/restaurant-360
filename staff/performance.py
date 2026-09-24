from datetime import timedelta
from django.utils import timezone

from .models import (
    Attendance,
    LeaveRequest,
    OrderStaffService,
    StaffTask,
)
from .services import ATTENDANCE_TIMEZONE


def get_employee_performance_metrics(employee, start_date=None, end_date=None, branch=None):
    """
    Calculate performance metrics strictly derived from real data:
    - Attendance rate & punctuality (attendance records, late counts, late minutes)
    - Hours worked & overtime
    - Waiter service volume (orders served, tables/sessions handled)
    - Waiter service speed (average READY to SERVED time)
    - Task completion rate
    """
    now = timezone.now()
    local_today = timezone.localtime(now, ATTENDANCE_TIMEZONE).date()

    if end_date is None:
        end_date = local_today
    if start_date is None:
        start_date = end_date - timedelta(days=30)

    end_date = min(end_date, local_today)

    # 1. Attendance & Hours
    attendance_qs = Attendance.objects.filter(
        employee=employee,
        restaurant_id=employee.user.restaurant_id,
        **({"branch": branch} if branch is not None else {}),
        work_date__range=(start_date, end_date),
    ).select_related("shift")

    attended_count = attendance_qs.count()
    late_count = 0
    total_late_minutes = 0
    total_worked_minutes = 0
    total_overtime_minutes = 0

    for rec in attendance_qs:
        if rec.is_late:
            late_count += 1
            total_late_minutes += rec.late_minutes
        total_worked_minutes += rec.worked_minutes
        total_overtime_minutes += rec.overtime_minutes

    worked_hours = round(total_worked_minutes / 60, 1)
    overtime_hours = round(total_overtime_minutes / 60, 1)

    # Expected working days (days in range from joining date)
    joining_date = getattr(employee, "joining_date", None)
    effective_start = max(start_date, joining_date) if joining_date else start_date
    expected_days = max(0, (end_date - effective_start).days + 1) if end_date >= effective_start else 0
    leave_dates = set()
    leaves = LeaveRequest.objects.filter(employee=employee, restaurant_id=employee.user.restaurant_id, status="approved", start_date__lte=end_date, end_date__gte=effective_start)
    if branch is not None:
        leaves = leaves.filter(branch=branch)
    for leave in leaves:
        if not leave.start_date or not leave.end_date:
            continue
        day = max(effective_start, leave.start_date)
        while day <= min(end_date, leave.end_date):
            leave_dates.add(day)
            day += timedelta(days=1)
    attended_dates = set(attendance_qs.values_list("work_date", flat=True))
    expected_days = max(0, expected_days - len(leave_dates - attended_dates))
    attendance_rate = min(100.0, round((attended_count / expected_days * 100), 1)) if expected_days else 0.0

    # 2. Waiter Service (Orders, Tables, Sessions, Speed)
    from django.db.models import Q
    service_qs = OrderStaffService.objects.filter(
        Q(waiter=employee) | Q(order_taken_by=employee) | Q(served_by=employee),
        order__restaurant_id=employee.user.restaurant_id,
        **({"order__branch": branch} if branch is not None else {}),
        assigned_at__date__range=(start_date, end_date),
    ).exclude(order__status="CANCELLED").select_related("order", "order__table", "order__table_session")

    total_orders_handled = service_qs.count()
    orders_served = service_qs.filter(
        Q(served_by=employee) | (Q(waiter=employee) & Q(served_at__isnull=False))
    ).distinct().count()
    orders_completed = service_qs.filter(completed_at__isnull=False).count()

    tables_handled = service_qs.filter(order__table__isnull=False).values("order__table_id").distinct().count()
    sessions_handled = service_qs.filter(order__table_session__isnull=False).values("order__table_session_id").distinct().count()

    # Unknown historical ready times stay unknown; assignment time is not a proxy.
    ready_to_served_diffs = []
    for service in service_qs.filter(served_at__isnull=False, ready_at__isnull=False).filter(
        Q(served_by=employee) | (Q(waiter=employee) & Q(served_by__isnull=True))
    ):
        s_at = service.served_at
        r_at = service.ready_at
        if timezone.is_naive(s_at):
            s_at = timezone.make_aware(s_at, timezone.get_current_timezone())
        if timezone.is_naive(r_at):
            r_at = timezone.make_aware(r_at, timezone.get_current_timezone())
        if s_at >= r_at:
            ready_to_served_diffs.append((s_at - r_at).total_seconds() / 60)

    avg_ready_to_served_mins = (
        round(sum(ready_to_served_diffs) / len(ready_to_served_diffs), 1)
        if ready_to_served_diffs else None
    )

    # 3. Tasks
    task_qs = StaffTask.objects.filter(
        employee=employee,
        restaurant_id=employee.user.restaurant_id,
        **({"branch": branch} if branch is not None else {}),
        work_date__range=(start_date, end_date),
    )
    total_tasks = task_qs.count()
    tasks_completed = task_qs.filter(is_completed=True).count()
    task_completion_rate = round((tasks_completed / total_tasks * 100), 1) if total_tasks else 100.0

    return {
        "start_date": start_date,
        "end_date": end_date,
        "attended_count": attended_count,
        "expected_days": expected_days,
        "attendance_rate": attendance_rate,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "worked_hours": worked_hours,
        "overtime_hours": overtime_hours,
        "total_orders_handled": total_orders_handled,
        "orders_served": orders_served,
        "orders_completed": orders_completed,
        "tables_handled": tables_handled,
        "sessions_handled": sessions_handled,
        "avg_ready_to_served_mins": avg_ready_to_served_mins,
        "service_timing_samples": len(ready_to_served_diffs),
        "total_tasks": total_tasks,
        "tasks_completed": tasks_completed,
        "task_completion_rate": task_completion_rate,
    }
