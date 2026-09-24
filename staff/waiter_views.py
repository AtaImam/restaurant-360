from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Avg, Count, F, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from orders.models import Order, OrderItem, TableSession
from orders.services import get_live_order_cutoff, transition_order_status
from restaurant.branch_services import get_active_branch
from restaurant.models import Branch, Floor, Restaurant, Table
from staff.access import staff_branch
from staff.models import (
    Attendance,
    DailyTableAssignment,
    EmployeeProfile,
    LeaveRequest,
    OrderStaffService,
    PayrollRecord,
    SalaryAdvance,
    Shift,
    StaffNotification,
    StaffTask,
)
from staff.operations import (
    ATTENDANCE_TIMEZONE,
    claim_order_service,
    complete_staff_task,
    ensure_order_staff_service,
    local_work_date,
)
from staff.performance import get_employee_performance_metrics
from staff.services import check_in_employee, check_out_employee


def _get_waiter_context(request):
    """
    Validate that user has waiter access, resolve employee profile,
    restaurant, and active branch with strict branch isolation.
    """
    user = request.user
    if not user.is_authenticated:
        raise PermissionDenied("Please log in.")
    if not user.is_active or not user.is_active_staff:
        raise PermissionDenied("Your staff account is inactive.")

    allowed_roles = {"waiter", "owner", "admin", "manager"}
    if user.role not in allowed_roles and not user.is_superuser:
        raise PermissionDenied("You do not have access to the Waiter Portal.")

    restaurant = getattr(user, "restaurant", None)
    profile = getattr(user, "employee_profile", None)
    if restaurant is None and profile:
        restaurant = profile.restaurant
    if restaurant is None:
        raise PermissionDenied("No restaurant associated with your account.")

    branch = user.branch
    if branch is None and profile:
        branch = profile.branch
    if branch is None:
        branch = get_active_branch(request, restaurant)
    if branch is None:
        branch = restaurant.branches.filter(is_active=True).first()

    return user, profile, branch, restaurant


# ---------------------------------------------------------------------------
# WAITER DASHBOARD
# ---------------------------------------------------------------------------

@login_required(login_url="/auth/login/")
@require_GET
def waiter_dashboard(request):
    user, profile, branch, restaurant = _get_waiter_context(request)
    now = timezone.now()
    today = local_work_date()
    live_cutoff = get_live_order_cutoff(now)

    # Shift & Attendance status
    today_attendance = None
    worked_hours_display = "0h 0m"
    overtime_hours_display = "0h 0m"
    is_checked_in = False
    is_checked_out = False

    if profile:
        today_attendance = Attendance.objects.filter(
            employee=profile,
            work_date=today,
        ).order_by("-check_in").first()

        if today_attendance:
            if today_attendance.check_out:
                is_checked_out = True
                wm = today_attendance.worked_minutes
                h, m = divmod(wm, 60)
                worked_hours_display = f"{h}h {m}m"
                otm = today_attendance.overtime_minutes
                oth, otm_rem = divmod(otm, 60)
                overtime_hours_display = f"{oth}h {otm_rem}m"
            else:
                is_checked_in = True
                wm = today_attendance.get_worked_minutes(now)
                h, m = divmod(wm, 60)
                worked_hours_display = f"{h}h {m}m"

    # Today's Shift
    today_shift = None
    if today_attendance and today_attendance.shift:
        today_shift = today_attendance.shift
    elif profile and profile.shift:
        today_shift = profile.shift

    # Assigned Tables
    assigned_table_ids = []
    assigned_tables = []
    if profile:
        assignments = DailyTableAssignment.objects.filter(
            waiter=profile,
            work_date=today,
            is_active=True,
        ).select_related("table", "table__floor")
        assigned_tables = [a.table for a in assignments]
        assigned_table_ids = [t.id for t in assigned_tables]

    # Active Orders
    branch_orders = Order.objects.filter(
        restaurant=restaurant,
        branch=branch,
        created_at__gte=live_cutoff,
    ).exclude(status__in=["COMPLETED", "CANCELLED"])

    # Waiter Active Orders: assigned to waiter via service OR table is waiter-assigned
    if profile:
        my_active_orders = branch_orders.filter(
            Q(staff_service__waiter=profile) | Q(table_id__in=assigned_table_ids)
        ).distinct()
    else:
        my_active_orders = branch_orders

    active_orders_count = my_active_orders.count()

    # Ready to Serve Orders in this branch
    ready_orders_qs = branch_orders.filter(status="READY").select_related(
        "table", "table__floor"
    ).prefetch_related("items__menu_item", "items__addons").order_by("status_changed_at", "created_at")

    ready_orders = []
    urgent_count = 0
    for order in ready_orders_qs:
        ready_since = order.status_changed_at or order.created_at
        elapsed_mins = int(max(0, (now - ready_since).total_seconds()) // 60)
        order.elapsed_minutes = elapsed_mins
        order.is_urgent = elapsed_mins >= 5
        if order.is_urgent:
            urgent_count += 1
        ready_orders.append(order)

    ready_to_serve_count = len(ready_orders)

    # Served Today
    served_today_count = 0
    if profile:
        served_today_count = OrderStaffService.objects.filter(
            waiter=profile,
            served_at__date=today,
        ).count()
    else:
        served_today_count = Order.objects.filter(
            restaurant=restaurant,
            branch=branch,
            status="SERVED",
            status_changed_at__date=today,
        ).count()

    # Today's Tasks
    tasks_qs = StaffTask.objects.filter(
        restaurant=restaurant,
        branch_id=branch.id if branch else None,
        work_date=today,
    )
    if profile:
        tasks_qs = tasks_qs.filter(employee=profile)

    tasks_list = list(tasks_qs.select_related("related_table", "related_order").order_by("is_completed", "-assigned_at"))
    pending_tasks_count = sum(1 for t in tasks_list if not t.is_completed)
    completed_tasks_count = sum(1 for t in tasks_list if t.is_completed)

    # Notifications for topbar
    notifications = StaffNotification.objects.filter(
        recipient=user,
    ).order_by("-created_at")[:10]
    unread_notifications_count = StaffNotification.objects.filter(
        recipient=user,
        is_read=False,
    ).count()

    context = {
        "waiter_user": user,
        "employee_profile": profile,
        "current_branch": branch,
        "restaurant": restaurant,
        "today_shift": today_shift,
        "today_attendance": today_attendance,
        "is_checked_in": is_checked_in,
        "is_checked_out": is_checked_out,
        "worked_hours_display": worked_hours_display,
        "overtime_hours_display": overtime_hours_display,
        "assigned_tables_count": len(assigned_tables),
        "assigned_tables": assigned_tables[:6],
        "active_orders_count": active_orders_count,
        "ready_to_serve_count": ready_to_serve_count,
        "urgent_ready_count": urgent_count,
        "served_today_count": served_today_count,
        "today_tasks_count": len(tasks_list),
        "pending_tasks_count": pending_tasks_count,
        "completed_tasks_count": completed_tasks_count,
        "tasks": tasks_list[:5],
        "ready_orders": ready_orders[:6],
        "notifications": notifications,
        "unread_notifications_count": unread_notifications_count,
        "active_tab": "dashboard",
    }
    return render(request, "waiter/dashboard.html", context)


# ---------------------------------------------------------------------------
# MY TABLES
# ---------------------------------------------------------------------------

@login_required(login_url="/auth/login/")
@require_GET
def waiter_tables(request):
    user, profile, branch, restaurant = _get_waiter_context(request)
    now = timezone.now()
    today = local_work_date()
    live_cutoff = get_live_order_cutoff(now)

    # Include outstanding cleaning from previous services as well as today's assignments.
    assigned_table_ids = []
    if profile:
        assigned_table_ids = list(
            DailyTableAssignment.objects.filter(
                waiter=profile,
                work_date=today,
                is_active=True,
            ).values_list("table_id", flat=True)
        )

    cleaning_sessions = TableSession.objects.filter(
        restaurant=restaurant, branch=branch, clean_needed=True,
    ).filter(
        Q(table_id__in=assigned_table_ids) | Q(orders__staff_service__waiter=profile)
    ).distinct() if profile else TableSession.objects.none()
    cleaning_by_table = {}
    for session in cleaning_sessions:
        cleaning_by_table.setdefault(session.table_id, []).append(session)
    visible_table_ids = set(assigned_table_ids) | set(cleaning_by_table)

    my_tables = Table.objects.filter(
        id__in=visible_table_ids,
        restaurant=restaurant,
        branch=branch,
        is_active=True,
    ).select_related("floor").order_by("floor__floor_number", "table_number")

    # Active orders for assigned tables (even if created by Manager, QR customer, or another staff)
    active_orders = (
        Order.objects.filter(
            restaurant=restaurant,
            branch=branch,
            table_id__in=visible_table_ids,
            created_at__gte=live_cutoff,
        )
        .filter(table_session__status=TableSession.STATUS_OPEN)
        .exclude(status__in=["COMPLETED", "CANCELLED"])
        .select_related(
            "table_session",
            "staff_service__waiter__user",
            "staff_service__order_taken_by__user",
            "staff_service__served_by__user",
        )
        .prefetch_related("items__menu_item")
        .order_by("-created_at")
    )

    orders_by_table = {}
    for o in active_orders:
        orders_by_table.setdefault(o.table_id, []).append(o)

    tables_data = []
    for t in my_tables:
        t.cleaning_sessions = cleaning_by_table.get(t.id, [])
        t_orders = orders_by_table.get(t.id, [])
        if not t_orders:
            tables_data.append({
                "table": t,
                "is_assigned": t.pk in assigned_table_ids,
                "has_active_order": False,
                "active_orders": [],
                "active_order": None,
                "ready_order": None,
                "unpaid_amount": Decimal("0.00"),
                "status_display": "Available",
                "service_state": "Available",
                "state_slug": "available",
                "badge_class": "badge-gray",
                "is_qr": False,
                "taken_by_name": "",
                "served_by_name": "",
                "elapsed_minutes": 0,
            })
            continue

        primary_order = t_orders[0]
        unpaid_amount = sum(
            (o.total_amount for o in t_orders if o.payment_status != "PAID"),
            Decimal("0.00")
        )
        ready_order = next((o for o in t_orders if o.status == "READY"), None)

        # Check QR order
        is_qr = any(
            (getattr(o, "order_type", None) == "DINE_IN" and getattr(getattr(o, "staff_service", None), "order_taken_by_id", None) is None)
            for o in t_orders
        )

        service_rec = getattr(primary_order, "staff_service", None)
        taken_by_name = ""
        if service_rec and service_rec.order_taken_by:
            taken_by_name = service_rec.order_taken_by.user.get_full_name() or service_rec.order_taken_by.user.username

        served_by_name = ""
        if service_rec and service_rec.served_by:
            served_by_name = service_rec.served_by.user.get_full_name() or service_rec.served_by.user.username

        elapsed_mins = int(max(0, (now - primary_order.created_at).total_seconds()) // 60)
        primary_order.elapsed_minutes = elapsed_mins

        # Cleaning is independent of these current-service states.
        if any(o.status == "READY" for o in t_orders):
            service_state = "Ready"
            state_slug = "ready"
            badge_class = "badge-success"
        elif any(o.status == "PREPARING" for o in t_orders):
            service_state = "Preparing"
            state_slug = "preparing"
            badge_class = "badge-warning"
        elif any(o.status in {"NEW", "ACCEPTED"} for o in t_orders):
            service_state = "New Order"
            state_slug = "new_order"
            badge_class = "badge-info"
        else:
            served_times = [
                getattr(getattr(o, "staff_service", None), "served_at", None) or o.status_changed_at or o.created_at
                for o in t_orders
            ]
            latest_served = max(served_times) if served_times else primary_order.created_at
            served_elapsed_mins = int(max(0, (now - latest_served).total_seconds()) // 60)
            if unpaid_amount > 0 and served_elapsed_mins > 15:
                service_state = "Payment Due"
                state_slug = "payment_due"
                badge_class = "badge-danger"
            else:
                service_state = "Dining"
                state_slug = "dining"
                badge_class = "badge-primary"

        tables_data.append({
            "table": t,
            "is_assigned": t.pk in assigned_table_ids,
            "has_active_order": True,
            "active_orders": t_orders,
            "active_order": primary_order,
            "ready_order": ready_order,
            "unpaid_amount": unpaid_amount,
            "status_display": service_state,
            "service_state": service_state,
            "state_slug": state_slug,
            "badge_class": badge_class,
            "is_qr": is_qr,
            "taken_by_name": taken_by_name,
            "served_by_name": served_by_name,
            "elapsed_minutes": elapsed_mins,
        })

    context = {
        "waiter_user": user,
        "employee_profile": profile,
        "current_branch": branch,
        "restaurant": restaurant,
        "tables_data": tables_data,
        "total_tables": len(tables_data),
        "assigned_count": len(assigned_table_ids),
        "unread_notifications_count": StaffNotification.objects.filter(recipient=user, is_read=False).count(),
        "active_tab": "tables",
    }
    return render(request, "waiter/tables.html", context)


# ---------------------------------------------------------------------------
# READY ORDERS
# ---------------------------------------------------------------------------

@login_required(login_url="/auth/login/")
@require_GET
def waiter_ready_orders(request):
    user, profile, branch, restaurant = _get_waiter_context(request)
    now = timezone.now()
    today = local_work_date()
    live_cutoff = get_live_order_cutoff(now)

    # Get assigned tables for waiter
    assigned_table_ids = set()
    if profile:
        assigned_table_ids = set(
            DailyTableAssignment.objects.filter(
                waiter=profile,
                work_date=today,
                is_active=True,
            ).values_list("table_id", flat=True)
        )

    ready_qs = (
        Order.objects.filter(
            restaurant=restaurant,
            branch=branch,
            status="READY",
            created_at__gte=live_cutoff,
        )
        .select_related("table", "table__floor", "staff_service__waiter__user")
        .prefetch_related("items__menu_item", "items__addons")
        .order_by("status_changed_at", "created_at")
    )

    ready_orders = []
    urgent_count = 0
    for order in ready_qs:
        ready_time = order.status_changed_at or order.created_at
        mins = int(max(0, (now - ready_time).total_seconds()) // 60)
        order.ready_duration_minutes = mins
        order.is_urgent = mins >= 5
        order.is_mine = (
            (order.table_id and order.table_id in assigned_table_ids)
            or (hasattr(order, "staff_service") and order.staff_service.waiter_id == getattr(profile, "id", None))
        )
        if order.is_urgent:
            urgent_count += 1
        ready_orders.append(order)

    context = {
        "waiter_user": user,
        "employee_profile": profile,
        "current_branch": branch,
        "restaurant": restaurant,
        "ready_orders": ready_orders,
        "urgent_count": urgent_count,
        "total_ready": len(ready_orders),
        "unread_notifications_count": StaffNotification.objects.filter(recipient=user, is_read=False).count(),
        "active_tab": "ready",
    }
    return render(request, "waiter/ready_orders.html", context)


# ---------------------------------------------------------------------------
# SERVE ORDER ACTION
# ---------------------------------------------------------------------------

@login_required(login_url="/auth/login/")
@require_POST
def waiter_serve_order_action(request, order_id):
    user, profile, branch, restaurant = _get_waiter_context(request)
    order = get_object_or_404(Order, pk=order_id, restaurant=restaurant)

    # Branch isolation
    if branch and order.branch_id != branch.id:
        raise PermissionDenied("Order belongs to another branch.")

    # Record served_by without altering official table assignment or service.waiter
    service = ensure_order_staff_service(order)
    if service is not None:
        fields = []
        if profile and service.served_by_id != profile.pk:
            service.served_by = profile
            fields.append("served_by")
        if service.served_at is None:
            service.served_at = timezone.now()
            fields.append("served_at")
        if fields:
            service.save(update_fields=fields)

    # Transition order status to SERVED
    try:
        transition_order_status(order, "SERVED", allowed_targets={"SERVED"})
        messages.success(request, f"Order #{order.id} marked as served!")
    except ValidationError as err:
        messages.error(request, " ".join(err.messages if hasattr(err, "messages") else [str(err)]))

    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url:
        return redirect(next_url)
    return redirect("waiter:ready_orders")


# ---------------------------------------------------------------------------
# CLEAN / CLOSE TABLE ACTION (After Settlement)
# ---------------------------------------------------------------------------

@login_required(login_url="/auth/login/")
@require_POST
def waiter_clean_table_action(request, table_id):
    user, profile, branch, restaurant = _get_waiter_context(request)
    table = get_object_or_404(Table, pk=table_id, restaurant=restaurant)

    if branch and table.branch_id != branch.id:
        raise PermissionDenied("Table belongs to another branch.")

    session_id = request.POST.get("session_id", "")
    if not session_id.isdecimal():
        messages.error(request, "Select the cleaning task to mark as Cleaned.")
        return redirect("waiter:tables")
    with transaction.atomic():
        session = get_object_or_404(
            TableSession.objects.select_for_update(), pk=session_id, table=table,
        )
        assigned = profile and DailyTableAssignment.objects.filter(
            waiter=profile, table=table, work_date=local_work_date(), is_active=True,
        ).exists()
        responsible = profile and session.orders.filter(staff_service__waiter=profile).exists()
        if not (assigned or responsible):
            raise PermissionDenied("This cleaning task is not assigned to you.")
        if session.clean_needed:
            session.clean_needed = False
            session.cleaned_at = timezone.now()
            session.cleaned_by = user
            session.save(update_fields=["clean_needed", "cleaned_at", "cleaned_by"])
            OrderStaffService.objects.filter(order__table_session=session).update(closed_by=user)
    messages.success(request, f"Table {table.table_number} marked as Cleaned.")
    return redirect("waiter:tables")


# ---------------------------------------------------------------------------
# CLAIM ORDER ACTION
# ---------------------------------------------------------------------------

@login_required(login_url="/auth/login/")
@require_POST
def waiter_claim_order_action(request, order_id):
    user, profile, branch, restaurant = _get_waiter_context(request)
    order = get_object_or_404(Order, pk=order_id, restaurant=restaurant)

    if branch and order.branch_id != branch.id:
        raise PermissionDenied("Order belongs to another branch.")

    try:
        service = claim_order_service(order.pk, user)
        messages.success(request, f"Order #{order.id} claimed successfully.")
    except (ValidationError, PermissionDenied) as err:
        messages.error(request, str(err))

    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url:
        return redirect(next_url)
    return redirect("waiter:tables")


# ---------------------------------------------------------------------------
# DAILY TASKS
# ---------------------------------------------------------------------------

@login_required(login_url="/auth/login/")
@require_GET
def waiter_tasks(request):
    user, profile, branch, restaurant = _get_waiter_context(request)
    today = local_work_date()

    tasks_qs = StaffTask.objects.filter(
        restaurant=restaurant,
        branch_id=branch.id if branch else None,
        work_date=today,
    )
    if profile:
        tasks_qs = tasks_qs.filter(employee=profile)

    status_filter = request.GET.get("status", "all")
    if status_filter == "pending":
        tasks_qs = tasks_qs.filter(is_completed=False)
    elif status_filter == "completed":
        tasks_qs = tasks_qs.filter(is_completed=True)

    tasks = list(tasks_qs.select_related("related_table", "related_order", "assigned_by").order_by("is_completed", "-assigned_at"))

    total_tasks = len(tasks)
    pending_count = StaffTask.objects.filter(
        restaurant=restaurant,
        branch_id=branch.id if branch else None,
        work_date=today,
        is_completed=False,
    )
    if profile:
        pending_count = pending_count.filter(employee=profile)
    pending_count = pending_count.count()

    context = {
        "waiter_user": user,
        "employee_profile": profile,
        "current_branch": branch,
        "restaurant": restaurant,
        "tasks": tasks,
        "status_filter": status_filter,
        "pending_count": pending_count,
        "completed_count": total_tasks - pending_count if status_filter == "all" else 0,
        "unread_notifications_count": StaffNotification.objects.filter(recipient=user, is_read=False).count(),
        "active_tab": "tasks",
    }
    return render(request, "waiter/tasks.html", context)


@login_required(login_url="/auth/login/")
@require_POST
def waiter_complete_task(request, task_id):
    user, profile, branch, restaurant = _get_waiter_context(request)
    task = get_object_or_404(StaffTask, pk=task_id, restaurant=restaurant)

    if profile and task.employee_id != profile.id and not user.is_superuser and user.role not in ["owner", "admin", "manager"]:
        raise PermissionDenied("You can only complete your own tasks.")

    try:
        completed_task, changed = complete_staff_task(actor=user, task_id=task_id)
        if changed:
            messages.success(request, f'Task "{completed_task.title}" completed!')
        else:
            messages.info(request, "Task is already completed.")
    except (PermissionDenied, ValidationError) as err:
        messages.error(request, str(err))

    return redirect("waiter:tasks")


# ---------------------------------------------------------------------------
# MY SHIFT
# ---------------------------------------------------------------------------

@login_required(login_url="/auth/login/")
@require_GET
def waiter_shift(request):
    user, profile, branch, restaurant = _get_waiter_context(request)
    today = local_work_date()

    today_attendance = None
    if profile:
        today_attendance = Attendance.objects.filter(
            employee=profile,
            work_date=today,
        ).select_related("shift").first()

    current_shift = None
    if today_attendance and today_attendance.shift:
        current_shift = today_attendance.shift
    elif profile and profile.shift:
        current_shift = profile.shift

    # Past attendance records showing shifts
    shift_history = []
    if profile:
        shift_history = Attendance.objects.filter(
            employee=profile,
        ).select_related("shift").order_by("-work_date")[:14]

    # Available shifts in branch/restaurant
    all_shifts = Shift.objects.filter(restaurant=restaurant, is_active=True).order_by("start_time")

    context = {
        "waiter_user": user,
        "employee_profile": profile,
        "current_branch": branch,
        "restaurant": restaurant,
        "current_shift": current_shift,
        "today_attendance": today_attendance,
        "shift_history": shift_history,
        "all_shifts": all_shifts,
        "unread_notifications_count": StaffNotification.objects.filter(recipient=user, is_read=False).count(),
        "active_tab": "shift",
    }
    return render(request, "waiter/shift.html", context)


# ---------------------------------------------------------------------------
# MY ATTENDANCE
# ---------------------------------------------------------------------------

@login_required(login_url="/auth/login/")
def waiter_attendance(request):
    user, profile, branch, restaurant = _get_waiter_context(request)
    now = timezone.now()
    today = local_work_date()

    today_record = None
    is_checked_in = False
    is_checked_out = False
    worked_hours_str = "0h 0m"
    overtime_hours_str = "0h 0m"

    if profile:
        today_record = Attendance.objects.filter(
            employee=profile,
            work_date=today,
        ).select_related("shift").order_by("-check_in").first()

        if today_record:
            if today_record.check_out:
                is_checked_out = True
                wm = today_record.worked_minutes
                h, m = divmod(wm, 60)
                worked_hours_str = f"{h}h {m}m"
                otm = today_record.overtime_minutes
                oth, otm_rem = divmod(otm, 60)
                overtime_hours_str = f"{oth}h {otm_rem}m"
            else:
                is_checked_in = True
                wm = today_record.get_worked_minutes(now)
                h, m = divmod(wm, 60)
                worked_hours_str = f"{h}h {m}m"

    history = []
    if profile:
        history = Attendance.objects.filter(
            employee=profile,
        ).select_related("shift").order_by("-work_date")[:30]

    context = {
        "waiter_user": user,
        "employee_profile": profile,
        "current_branch": branch,
        "restaurant": restaurant,
        "today_record": today_record,
        "is_checked_in": is_checked_in,
        "is_checked_out": is_checked_out,
        "worked_hours_str": worked_hours_str,
        "overtime_hours_str": overtime_hours_str,
        "history": history,
        "unread_notifications_count": StaffNotification.objects.filter(recipient=user, is_read=False).count(),
        "active_tab": "attendance",
    }
    return render(request, "waiter/attendance.html", context)


@login_required(login_url="/auth/login/")
@require_POST
def waiter_check_in(request):
    try:
        attendance = check_in_employee(request.user)
        messages.success(request, f"Checked in successfully at {timezone.localtime(attendance.check_in).strftime('%I:%M %p')}.")
    except ValidationError as err:
        messages.error(request, " ".join(err.messages if hasattr(err, "messages") else [str(err)]))
    return redirect("waiter:attendance")


@login_required(login_url="/auth/login/")
@require_POST
def waiter_check_out(request):
    try:
        attendance = check_out_employee(request.user)
        messages.success(request, f"Checked out successfully at {timezone.localtime(attendance.check_out).strftime('%I:%M %p')}.")
    except ValidationError as err:
        messages.error(request, " ".join(err.messages if hasattr(err, "messages") else [str(err)]))
    return redirect("waiter:attendance")


# ---------------------------------------------------------------------------
# MY LEAVE
# ---------------------------------------------------------------------------

@login_required(login_url="/auth/login/")
def waiter_leave(request):
    user, profile, branch, restaurant = _get_waiter_context(request)

    if request.method == "POST":
        leave_type = request.POST.get("leave_type")
        start_date_str = request.POST.get("start_date")
        end_date_str = request.POST.get("end_date")
        reason = request.POST.get("reason", "").strip()

        if not profile:
            messages.error(request, "Employee profile required to request leave.")
            return redirect("waiter:leave")

        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
            if end_date < start_date:
                raise ValidationError("End date cannot be before start date.")
            if not reason:
                raise ValidationError("Please provide a reason for leave.")

            LeaveRequest.objects.create(
                employee=profile,
                restaurant=restaurant,
                branch=branch,
                leave_type=leave_type,
                start_date=start_date,
                end_date=end_date,
                reason=reason,
                status="pending",
            )
            messages.success(request, "Leave request submitted successfully.")
            return redirect("waiter:leave")
        except (ValueError, ValidationError) as err:
            messages.error(request, str(err))

    leaves = []
    if profile:
        leaves = LeaveRequest.objects.filter(employee=profile).order_by("-created_at")

    context = {
        "waiter_user": user,
        "employee_profile": profile,
        "current_branch": branch,
        "restaurant": restaurant,
        "leaves": leaves,
        "leave_types": LeaveRequest.LEAVE_TYPE_CHOICES,
        "unread_notifications_count": StaffNotification.objects.filter(recipient=user, is_read=False).count(),
        "active_tab": "leave",
    }
    return render(request, "waiter/leave.html", context)


# ---------------------------------------------------------------------------
# MY SERVICE HISTORY
# ---------------------------------------------------------------------------

@login_required(login_url="/auth/login/")
@require_GET
def waiter_service_history(request):
    user, profile, branch, restaurant = _get_waiter_context(request)
    period = request.GET.get("period", "all")
    now = timezone.now()

    services_qs = OrderStaffService.objects.none()
    if profile:
        services_qs = OrderStaffService.objects.filter(
            Q(waiter=profile) | Q(order_taken_by=profile) | Q(served_by=profile) | Q(handover_by=profile)
        ).select_related(
            "order", "order__table", "order__table__floor", "waiter__user", "order_taken_by__user", "served_by__user"
        ).distinct().order_by("-assigned_at")

        if period == "today":
            today = local_work_date()
            services_qs = services_qs.filter(assigned_at__date=today)
        elif period == "week":
            services_qs = services_qs.filter(assigned_at__gte=now - timedelta(days=7))
        elif period == "month":
            services_qs = services_qs.filter(assigned_at__gte=now - timedelta(days=30))

    services_list = []
    for s in services_qs[:100]:
        duration_mins = None
        if s.served_at and s.assigned_at:
            duration_mins = int((s.served_at - s.assigned_at).total_seconds() // 60)
        s.duration_minutes = duration_mins
        services_list.append(s)

    # Summary metrics
    total_served = sum(
        1 for s in services_list
        if s.served_at and (s.served_by_id == getattr(profile, "id", None) or s.waiter_id == getattr(profile, "id", None))
    )
    avg_speed = None
    speeds = [s.duration_minutes for s in services_list if s.duration_minutes is not None]
    if speeds:
        avg_speed = round(sum(speeds) / len(speeds), 1)

    context = {
        "waiter_user": user,
        "employee_profile": profile,
        "current_branch": branch,
        "restaurant": restaurant,
        "services": services_list,
        "total_served": total_served,
        "avg_speed": avg_speed,
        "period": period,
        "unread_notifications_count": StaffNotification.objects.filter(recipient=user, is_read=False).count(),
        "active_tab": "service_history",
    }
    return render(request, "waiter/history.html", context)


# ---------------------------------------------------------------------------
# MY PERFORMANCE
# ---------------------------------------------------------------------------

@login_required(login_url="/auth/login/")
@require_GET
def waiter_performance(request):
    user, profile, branch, restaurant = _get_waiter_context(request)
    metrics = None
    if profile:
        metrics = get_employee_performance_metrics(profile)

    context = {
        "waiter_user": user,
        "employee_profile": profile,
        "current_branch": branch,
        "restaurant": restaurant,
        "metrics": metrics,
        "unread_notifications_count": StaffNotification.objects.filter(recipient=user, is_read=False).count(),
        "notifications": StaffNotification.objects.filter(recipient=user).order_by("-created_at")[:10],
        "active_tab": "performance",
    }
    return render(request, "waiter/performance.html", context)


# ---------------------------------------------------------------------------
# PAYROLL: MY SALARY
# ---------------------------------------------------------------------------

@login_required(login_url="/auth/login/")
@require_GET
def waiter_salary(request):
    user, profile, branch, restaurant = _get_waiter_context(request)
    now = timezone.now()

    # Current month attendance summary
    current_month_worked_hours = Decimal("0.00")
    current_month_ot_hours = Decimal("0.00")
    if profile:
        monthly_att = Attendance.objects.filter(
            employee=profile,
            work_date__year=now.year,
            work_date__month=now.month,
        ).select_related("shift")
        total_wm = sum(rec.worked_minutes for rec in monthly_att)
        total_otm = sum(rec.overtime_minutes for rec in monthly_att)
        current_month_worked_hours = Decimal(str(round(total_wm / 60.0, 2)))
        current_month_ot_hours = Decimal(str(round(total_otm / 60.0, 2)))

    basic_salary = profile.basic_salary if profile else Decimal("0.00")
    hourly_rate = getattr(profile, "hourly_rate", None) or ((basic_salary / Decimal("208")).quantize(Decimal("0.01")) if basic_salary else Decimal("0.00"))
    overtime_rate = (hourly_rate * Decimal("1.5")).quantize(Decimal("0.01")) if hourly_rate else Decimal("0.00")

    # Estimate current month earnings
    estimated_ot_pay = current_month_ot_hours * overtime_rate
    estimated_net = basic_salary + estimated_ot_pay

    context = {
        "waiter_user": user,
        "employee_profile": profile,
        "current_branch": branch,
        "restaurant": restaurant,
        "basic_salary": basic_salary,
        "hourly_rate": hourly_rate,
        "overtime_rate": overtime_rate,
        "worked_hours_month": current_month_worked_hours,
        "ot_hours_month": current_month_ot_hours,
        "estimated_ot_pay": estimated_ot_pay,
        "estimated_net": estimated_net,
        "unread_notifications_count": StaffNotification.objects.filter(recipient=user, is_read=False).count(),
        "active_tab": "salary",
    }
    return render(request, "waiter/payroll.html", context)


# ---------------------------------------------------------------------------
# PAYROLL: SALARY ADVANCE
# ---------------------------------------------------------------------------

@login_required(login_url="/auth/login/")
def waiter_salary_advance(request):
    user, profile, branch, restaurant = _get_waiter_context(request)

    if request.method == "POST":
        amount_str = request.POST.get("amount", "").strip()
        reason = request.POST.get("reason", "").strip()

        if not profile:
            messages.error(request, "Employee profile required to request salary advance.")
            return redirect("waiter:salary_advance")

        try:
            amount = Decimal(amount_str)
            if amount <= Decimal("0.00"):
                raise ValidationError("Amount must be greater than zero.")
            if not reason:
                raise ValidationError("Please specify the reason for advance.")

            SalaryAdvance.objects.create(
                employee=profile,
                restaurant=restaurant,
                branch=branch,
                amount=amount,
                reason=reason,
                status="pending",
            )
            messages.success(request, f"Salary advance request of {amount} submitted.")
            return redirect("waiter:salary_advance")
        except (ValueError, ValidationError) as err:
            messages.error(request, str(err))

    advances = []
    if profile:
        advances = SalaryAdvance.objects.filter(employee=profile).order_by("-created_at")

    context = {
        "waiter_user": user,
        "employee_profile": profile,
        "current_branch": branch,
        "restaurant": restaurant,
        "advances": advances,
        "unread_notifications_count": StaffNotification.objects.filter(recipient=user, is_read=False).count(),
        "active_tab": "salary_advance",
    }
    return render(request, "waiter/advance.html", context)


# ---------------------------------------------------------------------------
# PAYROLL: PAYROLL HISTORY
# ---------------------------------------------------------------------------

@login_required(login_url="/auth/login/")
@require_GET
def waiter_payroll_history(request):
    user, profile, branch, restaurant = _get_waiter_context(request)

    records = []
    if profile:
        records = PayrollRecord.objects.filter(
            employee=profile
        ).order_by("-month")

    context = {
        "waiter_user": user,
        "employee_profile": profile,
        "current_branch": branch,
        "restaurant": restaurant,
        "records": records,
        "unread_notifications_count": StaffNotification.objects.filter(recipient=user, is_read=False).count(),
        "active_tab": "payroll_history",
    }
    return render(request, "waiter/payroll_history.html", context)


# ---------------------------------------------------------------------------
# NOTIFICATIONS (TOPBAR)
# ---------------------------------------------------------------------------

@login_required(login_url="/auth/login/")
@require_POST
def waiter_mark_notification_read(request, notification_id):
    StaffNotification.objects.filter(
        pk=notification_id,
        recipient=request.user,
        is_read=False,
    ).update(is_read=True, read_at=timezone.now())

    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url:
        return redirect(next_url)
    return redirect("waiter:dashboard")


@login_required(login_url="/auth/login/")
@require_POST
def waiter_mark_all_notifications_read(request):
    StaffNotification.objects.filter(
        recipient=request.user,
        is_read=False,
    ).update(is_read=True, read_at=timezone.now())

    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url:
        return redirect(next_url)
    return redirect("waiter:dashboard")
