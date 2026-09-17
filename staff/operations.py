from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from orders.models import Order
from restaurant.models import Table

from .models import (
    Attendance,
    DailyTableAssignment,
    EmployeeProfile,
    OrderStaffService,
    StaffNotification,
    StaffTask,
)
from .services import ATTENDANCE_TIMEZONE


User = get_user_model()

MANAGEMENT_ROLES = {
    "admin",
    "owner",
    "manager",
}

KITCHEN_ROLES = {
    "chief",
    "kitchen_manager",
    "bar_manager",
}


def local_work_date():
    return timezone.localtime(
        timezone.now(),
        ATTENDANCE_TIMEZONE,
    ).date()


def require_operations_manager(actor):
    if not actor.is_authenticated:
        raise PermissionDenied("Please log in.")

    if not actor.is_active or not actor.is_active_staff:
        raise PermissionDenied(
            "Your staff account is inactive."
        )

    if not (
        actor.is_superuser
        or actor.role in MANAGEMENT_ROLES
    ):
        raise PermissionDenied(
            "You cannot manage staff operations."
        )


def ensure_restaurant_access(actor, restaurant_id):
    require_operations_manager(actor)

    if actor.is_superuser or actor.role == "admin":
        return

    if actor.restaurant_id != restaurant_id:
        raise PermissionDenied(
            "You cannot manage another restaurant."
        )


def _create_notification(
    *,
    restaurant,
    recipient,
    notification_type,
    title,
    message,
    event_key,
    order=None,
):
    notification, _ = StaffNotification.objects.get_or_create(
        recipient=recipient,
        event_key=event_key,
        defaults={
            "restaurant": restaurant,
            "order": order,
            "notification_type": notification_type,
            "title": title,
            "message": message,
        },
    )

    return notification


def _management_users(restaurant):
    return (
        User.objects
        .filter(
            is_active=True,
            is_active_staff=True,
        )
        .filter(
            Q(is_superuser=True)
            | Q(role="admin")
            | Q(
                role__in=["owner", "manager"],
                restaurant=restaurant,
            )
        )
        .distinct()
    )


def _notify_management(
    *,
    restaurant,
    notification_type,
    title,
    message,
    event_key,
    order=None,
):
    for user in _management_users(restaurant):
        _create_notification(
            restaurant=restaurant,
            recipient=user,
            notification_type=notification_type,
            title=title,
            message=message,
            event_key=event_key,
            order=order,
        )


def _notify_present_kitchen_staff(order):
    work_date = timezone.localtime(
        order.created_at,
        ATTENDANCE_TIMEZONE,
    ).date()

    user_ids = (
        Attendance.objects
        .filter(
            restaurant=order.restaurant,
            work_date=work_date,
            check_out__isnull=True,
            employee__user__role__in=KITCHEN_ROLES,
            employee__user__is_active=True,
            employee__user__is_active_staff=True,
        )
        .values_list(
            "employee__user_id",
            flat=True,
        )
        .distinct()
    )

    table_label = (
        f"Table {order.table.table_number}"
        if order.table_id
        else "Takeaway"
    )

    for user in User.objects.filter(pk__in=user_ids):
        _create_notification(
            restaurant=order.restaurant,
            recipient=user,
            order=order,
            notification_type="new_order",
            title=f"New Order #{order.pk}",
            message=(
                f"{table_label} placed a new order. "
                "Review it in the kitchen display."
            ),
            event_key=f"order:{order.pk}:new:kitchen",
        )


@transaction.atomic
def assign_table_to_waiter(
    *,
    actor,
    attendance_id,
    table_id,
):
    attendance = (
        Attendance.objects
        .select_for_update()
        .select_related(
            "employee__user",
            "restaurant",
        )
        .get(pk=attendance_id)
    )

    ensure_restaurant_access(
        actor,
        attendance.restaurant_id,
    )

    if attendance.work_date != local_work_date():
        raise ValidationError(
            "Tables can only be assigned to today's attendance."
        )

    if attendance.check_out is not None:
        raise ValidationError(
            "This employee has already checked out."
        )

    if attendance.employee.user.role != "waiter":
        raise ValidationError(
            "Tables can only be assigned to a waiter."
        )

    table = get_table_for_assignment(
        restaurant_id=attendance.restaurant_id,
        table_id=table_id,
    )

    existing = (
        DailyTableAssignment.objects
        .select_for_update()
        .filter(
            table=table,
            work_date=attendance.work_date,
            is_active=True,
        )
        .first()
    )

    if (
        existing is not None
        and existing.waiter_id == attendance.employee_id
    ):
        return existing, False

    if existing is not None:
        existing.is_active = False
        existing.ended_at = timezone.now()
        existing.save(
            update_fields=[
                "is_active",
                "ended_at",
            ]
        )

    assignment = DailyTableAssignment.objects.create(
        work_date=attendance.work_date,
        restaurant=attendance.restaurant,
        table=table,
        waiter=attendance.employee,
        attendance=attendance,
        assigned_by=actor,
    )

    _create_notification(
        restaurant=attendance.restaurant,
        recipient=attendance.employee.user,
        notification_type="general",
        title=f"Table {table.table_number} assigned",
        message=(
            f"You are assigned to Table "
            f"{table.table_number} for today."
        ),
        event_key=(
            f"table-assignment:{assignment.pk}:"
            f"{attendance.employee.user_id}"
        ),
    )

    return assignment, True


def get_table_for_assignment(
    *,
    restaurant_id,
    table_id,
):
    table = (
        Table.objects
        .filter(
            pk=table_id,
            restaurant_id=restaurant_id,
            is_active=True,
        )
        .first()
    )

    if table is None:
        raise ValidationError(
            "The selected table is unavailable."
        )

    return table


@transaction.atomic
def assign_staff_task(
    *,
    actor,
    attendance_id,
    title,
    instructions="",
    related_order_id=None,
    related_table_id=None,
):
    attendance = (
        Attendance.objects
        .select_for_update()
        .select_related(
            "employee__user",
            "restaurant",
        )
        .get(pk=attendance_id)
    )

    ensure_restaurant_access(
        actor,
        attendance.restaurant_id,
    )

    if attendance.work_date != local_work_date():
        raise ValidationError(
            "Tasks can only be assigned to today's attendance."
        )

    if attendance.check_out is not None:
        raise ValidationError(
            "This employee has already checked out."
        )

    title = title.strip()
    instructions = instructions.strip()

    if not title:
        raise ValidationError(
            "Enter a task title."
        )

    order = None

    if related_order_id:
        order = (
            Order.objects
            .filter(
                pk=related_order_id,
                restaurant_id=attendance.restaurant_id,
            )
            .first()
        )

        if order is None:
            raise ValidationError(
                "The selected order is unavailable."
            )

    table = None

    if related_table_id:
        table = get_table_for_assignment(
            restaurant_id=attendance.restaurant_id,
            table_id=related_table_id,
        )

    task = StaffTask.objects.create(
        restaurant=attendance.restaurant,
        work_date=attendance.work_date,
        employee=attendance.employee,
        attendance=attendance,
        related_order=order,
        related_table=table,
        title=title,
        instructions=instructions,
        assigned_by=actor,
    )

    _create_notification(
        restaurant=attendance.restaurant,
        recipient=attendance.employee.user,
        order=order,
        notification_type="task_assigned",
        title="New task assigned",
        message=title,
        event_key=(
            f"staff-task:{task.pk}:"
            f"{attendance.employee.user_id}"
        ),
    )

    return task


@transaction.atomic
def complete_staff_task(*, actor, task_id):
    task = (
        StaffTask.objects
        .select_for_update()
        .select_related(
            "employee__user",
            "restaurant",
        )
        .get(pk=task_id)
    )

    is_assigned_employee = (
        task.employee.user_id == actor.pk
    )

    is_manager = (
        actor.is_superuser
        or actor.role in MANAGEMENT_ROLES
    )

    if not is_assigned_employee and not is_manager:
        raise PermissionDenied(
            "You cannot complete this task."
        )

    if is_manager:
        ensure_restaurant_access(
            actor,
            task.restaurant_id,
        )

    if task.is_completed:
        return task, False

    task.is_completed = True
    task.completed_at = timezone.now()
    task.save(
        update_fields=[
            "is_completed",
            "completed_at",
        ]
    )

    return task, True


def _find_table_assignment(order):
    if not order.table_id:
        return None

    work_date = timezone.localtime(
        order.created_at,
        ATTENDANCE_TIMEZONE,
    ).date()

    return (
        DailyTableAssignment.objects
        .select_related(
            "waiter__user",
            "table",
        )
        .filter(
            restaurant=order.restaurant,
            table=order.table,
            work_date=work_date,
            is_active=True,
        )
        .first()
    )


def ensure_order_staff_service(order):
    try:
        return order.staff_service
    except OrderStaffService.DoesNotExist:
        assignment = _find_table_assignment(order)

        if assignment is None:
            return None

        service, _ = OrderStaffService.objects.get_or_create(
            order=order,
            defaults={
                "waiter": assignment.waiter,
                "table_assignment": assignment,
            },
        )

        return service


@transaction.atomic
def register_new_order(order_id):
    order = (
        Order.objects
        .select_related(
            "restaurant",
            "table",
        )
        .get(pk=order_id)
    )

    table_label = (
        f"Table {order.table.table_number}"
        if order.table_id
        else "Takeaway"
    )

    _notify_present_kitchen_staff(order)

    service = ensure_order_staff_service(order)

    if service is not None:
        _create_notification(
            restaurant=order.restaurant,
            recipient=service.waiter.user,
            order=order,
            notification_type="new_order",
            title=f"New Order #{order.pk}",
            message=(
                f"{table_label} placed a new order. "
                "You are the assigned waiter."
            ),
            event_key=(
                f"order:{order.pk}:new:"
                f"waiter:{service.waiter.user_id}"
            ),
        )

    _notify_management(
        restaurant=order.restaurant,
        order=order,
        notification_type="new_order",
        title=f"New Order #{order.pk}",
        message=f"{table_label} placed a new order.",
        event_key=f"order:{order.pk}:new:management",
    )

    return order


@transaction.atomic
def handle_order_status_change(
    *,
    order_id,
    previous_status,
    new_status,
):
    if previous_status == new_status:
        return

    order = (
        Order.objects
        .select_related(
            "restaurant",
            "table",
        )
        .get(pk=order_id)
    )

    service = ensure_order_staff_service(order)

    table_label = (
        f"Table {order.table.table_number}"
        if order.table_id
        else "Takeaway"
    )

    if new_status == "READY":
        if service is not None:
            _create_notification(
                restaurant=order.restaurant,
                recipient=service.waiter.user,
                order=order,
                notification_type="food_ready",
                title=f"Order #{order.pk} is ready",
                message=(
                    f"{table_label} food is ready. "
                    "Collect it and serve the customer."
                ),
                event_key=(
                    f"order:{order.pk}:ready:"
                    f"waiter:{service.waiter.user_id}"
                ),
            )

        _notify_management(
            restaurant=order.restaurant,
            order=order,
            notification_type="food_ready",
            title=f"Order #{order.pk} is ready",
            message=f"{table_label} food is ready.",
            event_key=f"order:{order.pk}:ready:management",
        )

    elif new_status == "SERVED":
        if service is not None and service.served_at is None:
            service.served_at = timezone.now()
            service.save(update_fields=["served_at"])

        _notify_management(
            restaurant=order.restaurant,
            order=order,
            notification_type="order_served",
            title=f"Order #{order.pk} served",
            message=f"{table_label} order was served.",
            event_key=f"order:{order.pk}:served:management",
        )

    elif new_status == "COMPLETED":
        if service is not None:
            if service.completed_at is None:
                service.completed_at = timezone.now()
                service.save(
                    update_fields=["completed_at"]
                )

            _create_notification(
                restaurant=order.restaurant,
                recipient=service.waiter.user,
                order=order,
                notification_type="order_completed",
                title=f"Order #{order.pk} completed",
                message=(
                    f"{table_label} service is complete. "
                    "You are available for the next service."
                ),
                event_key=(
                    f"order:{order.pk}:completed:"
                    f"waiter:{service.waiter.user_id}"
                ),
            )

        _notify_management(
            restaurant=order.restaurant,
            order=order,
            notification_type="order_completed",
            title=f"Order #{order.pk} completed",
            message=f"{table_label} service is complete.",
            event_key=f"order:{order.pk}:completed:management",
        )
