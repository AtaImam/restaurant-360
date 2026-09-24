from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from orders.models import Order
from orders.services import transition_order_status
from restaurant.models import Restaurant, Table

from .access import staff_branch, staff_queryset
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.db.models import Q
from datetime import timedelta

from .models import (
    Attendance,
    DailyTableAssignment,
    OrderStaffService,
    StaffNotification,
    StaffTask,
)
from .operations import (
    MANAGEMENT_ROLES,
    assign_staff_task,
    assign_table_to_waiter,
    claim_order_service,
    complete_staff_task,
    ensure_restaurant_access,
    local_work_date,
    require_operations_manager,
)
from .operations_forms import (
    StaffTaskAssignmentForm,
    TableAssignmentForm,
)


@login_required
@require_GET
def daily_operations(request):
    branch = staff_branch(request)
    restaurant = request.user.restaurant
    work_date = local_work_date()

    present_records = (
        staff_queryset(request, Attendance)
        .filter(
            restaurant=restaurant,
            work_date__in=[work_date - timedelta(days=1), work_date, work_date + timedelta(days=1)],
        )
        .select_related(
            "employee__user",
            "employee__shift",
        )
        .order_by(
            "employee__user__role",
            "employee__user__first_name",
            "employee__employee_id",
        )
    )

    present_records = present_records.filter(Q(work_date=work_date) | Q(check_out__isnull=True))
    open_attendance = present_records.filter(
        check_out__isnull=True
    )

    table_assignments = (
        staff_queryset(request, DailyTableAssignment)
        .filter(
            restaurant=restaurant,
            work_date__in=[work_date - timedelta(days=1), work_date, work_date + timedelta(days=1)],
            is_active=True,
            attendance__check_out__isnull=True,
        )
        .select_related(
            "table",
            "waiter__user",
            "attendance",
        )
        .order_by("table__table_number")
    )

    assigned_table_ids = table_assignments.values_list(
        "table_id",
        flat=True,
    )

    unassigned_tables = (
        staff_queryset(request, Table)
        .filter(
            restaurant=restaurant,
            is_active=True,
        )
        .exclude(pk__in=assigned_table_ids)
        .order_by("table_number")
    )

    tasks = (
        staff_queryset(request, StaffTask)
        .filter(
            restaurant=restaurant,
            work_date__in=[work_date - timedelta(days=1), work_date, work_date + timedelta(days=1)],
        )
        .select_related(
            "employee__user",
            "attendance",
            "related_order",
            "related_table",
            "assigned_by",
        )
        .order_by(
            "is_completed",
            "-assigned_at",
        )
    )

    active_services = (
        staff_queryset(request, OrderStaffService)
        .filter(
            order__restaurant=restaurant,
        )
        .exclude(order__status__in=["COMPLETED", "CANCELLED"])
        .select_related(
            "order__table",
            "waiter__user",
            "table_assignment",
        )
        .order_by("-assigned_at")
    )

    is_waiter = request.user.role == "waiter" and not (
        request.user.is_superuser or request.user.role in MANAGEMENT_ROLES
    )
    if is_waiter:
        active_services = active_services.filter(waiter__user=request.user)
        tasks = tasks.filter(employee__user=request.user)
        table_assignments = table_assignments.filter(waiter__user=request.user)

    notifications = (
        staff_queryset(request, StaffNotification)
        .filter(
            recipient=request.user,
            restaurant=restaurant,
        )
        .select_related(
            "order",
            "order__table",
        )
        .order_by("-created_at")[:10]
    )

    table_form = TableAssignmentForm(
        restaurant=restaurant,
        work_date=work_date,
        branch=branch,
    )

    task_form = StaffTaskAssignmentForm(
        restaurant=restaurant,
        work_date=work_date,
        branch=branch,
    )

    context = {
        "restaurant": restaurant,
        "branch": branch,
        "available_waiters": staff_queryset(request, get_user_model()).filter(role="waiter", is_active=True, is_active_staff=True, employee_profile__isnull=False),
        "unassigned_orders": staff_queryset(request, Order).filter(staff_service__isnull=True).exclude(status__in=["SERVED", "COMPLETED", "CANCELLED"]).order_by("-created_at")[:50],
        "work_date": work_date,
        "is_waiter": is_waiter,
        "can_manage": not is_waiter,
        "present_records": present_records,
        "open_attendance": open_attendance,
        "table_assignments": table_assignments,
        "unassigned_tables": unassigned_tables,
        "tasks": tasks,
        "active_services": active_services,
        "notifications": notifications,
        "table_form": table_form,
        "task_form": task_form,
        "present_count": present_records.count(),
        "working_count": open_attendance.count(),
        "assigned_table_count": table_assignments.count(),
        "unassigned_table_count": unassigned_tables.count(),
        "open_task_count": tasks.filter(
            is_completed=False
        ).count(),
    }

    return render(
        request,
        "staff/daily_operations.html",
        context,
    )


@login_required
@require_POST
def daily_table_assign(request):
    branch = staff_branch(request)
    restaurant = request.user.restaurant
    work_date = local_work_date()

    form = TableAssignmentForm(
        request.POST,
        restaurant=restaurant,
        work_date=work_date,
        branch=branch,
    )

    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)

        return redirect("staff:daily_operations")

    attendance = form.cleaned_data["attendance"]
    table = form.cleaned_data["table"]

    try:
        assignment, created = assign_table_to_waiter(
            actor=request.user,
            attendance_id=attendance.pk,
            table_id=table.pk,
        )

    except (
        Attendance.DoesNotExist,
        ValidationError,
    ) as error:
        messages.error(
            request,
            " ".join(getattr(error, "messages", [str(error)])),
        )

    else:
        if created:
            messages.success(
                request,
                (
                    f"Table {assignment.table.table_number} "
                    f"assigned to "
                    f"{assignment.waiter.user.get_full_name() or assignment.waiter.user.username}."
                ),
            )
        else:
            messages.info(
                request,
                "This table is already assigned to that waiter.",
            )

    return redirect("staff:daily_operations")


@login_required
@require_POST
def daily_task_assign(request):
    branch = staff_branch(request)
    restaurant = request.user.restaurant
    work_date = local_work_date()

    form = StaffTaskAssignmentForm(
        request.POST,
        restaurant=restaurant,
        work_date=work_date,
        branch=branch,
    )

    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)

        return redirect("staff:daily_operations")

    attendance = form.cleaned_data["attendance"]
    related_order = form.cleaned_data["related_order"]
    related_table = form.cleaned_data["related_table"]

    try:
        task = assign_staff_task(
            actor=request.user,
            attendance_id=attendance.pk,
            title=form.cleaned_data["title"],
            instructions=form.cleaned_data["instructions"],
            related_order_id=(
                related_order.pk
                if related_order is not None
                else None
            ),
            related_table_id=(
                related_table.pk
                if related_table is not None
                else None
            ),
        )

    except (
        Attendance.DoesNotExist,
        ValidationError,
    ) as error:
        messages.error(
            request,
            " ".join(getattr(error, "messages", [str(error)])),
        )

    else:
        messages.success(
            request,
            (
                f"Task assigned to "
                f"{task.employee.user.get_full_name() or task.employee.user.username}."
            ),
        )

    return redirect("staff:daily_operations")


@login_required
@require_POST
def daily_task_complete(request, task_id):
    get_object_or_404(staff_queryset(request, StaffTask), pk=task_id)
    try:
        task, changed = complete_staff_task(
            actor=request.user,
            task_id=task_id,
        )

    except StaffTask.DoesNotExist:
        messages.error(
            request,
            "Task not found.",
        )

    except (
        PermissionDenied,
        ValidationError,
    ) as error:
        messages.error(
            request,
            " ".join(getattr(error, "messages", [str(error)])),
        )

    else:
        if changed:
            messages.success(
                request,
                f'Task "{task.title}" completed.',
            )
        else:
            messages.info(
                request,
                "This task is already completed.",
            )

    return redirect("staff:daily_operations")


@login_required
@require_POST
def waiter_serve_order(request, order_id):
    service = get_object_or_404(
        staff_queryset(request, OrderStaffService).select_related(
            "order",
            "waiter__user",
            "order__restaurant",
        ),
        order_id=order_id,
    )

    is_assigned_waiter = (
        service.waiter.user_id == request.user.pk
    )

    is_manager = (
        request.user.is_superuser
        or request.user.role in MANAGEMENT_ROLES
    )

    if not is_assigned_waiter and not is_manager:
        raise PermissionDenied(
            "This order is not assigned to you."
        )

    if is_manager:
        ensure_restaurant_access(
            request.user,
            service.order.restaurant_id,
        )

    try:
        transition_order_status(
            service.order,
            "SERVED",
            allowed_targets={"SERVED"},
        )

    except ValidationError as error:
        messages.error(
            request,
            " ".join(error.messages),
        )

    else:
        messages.success(
            request,
            f"Order #{service.order_id} marked as served.",
        )

    next_url = request.POST.get("next", "").strip()

    if next_url == "daily_operations":
        return redirect("staff:daily_operations")

    return redirect("staff:daily_operations")


@login_required
@require_POST
def waiter_claim_order(request, order_id):
    get_object_or_404(staff_queryset(request, Order), pk=order_id)
    waiter = request.user
    if request.user.role in MANAGEMENT_ROLES and request.POST.get("waiter_id"):
        waiter = get_object_or_404(staff_queryset(request, get_user_model()), pk=request.POST["waiter_id"], role="waiter", is_active=True, is_active_staff=True)
    try:
        service = claim_order_service(order_id, waiter)
    except (ValidationError, PermissionDenied) as error:
        messages.error(
            request,
            " ".join(getattr(error, "messages", [str(error)])),
        )
    else:
        messages.success(
            request,
            f"Order #{service.order_id} claimed successfully.",
        )

    next_url = request.POST.get("next", "").strip()
    if next_url == "daily_operations":
        return redirect("staff:daily_operations")

    return redirect("staff:daily_operations")

@login_required
@require_POST
def notification_mark_read(request, notification_id):
    notification = get_object_or_404(staff_queryset(request, StaffNotification), pk=notification_id, recipient=request.user)
    staff_queryset(request, StaffNotification).filter(pk=notification.pk, is_read=False).update(is_read=True, read_at=timezone.now())
    return redirect("staff:daily_operations")
