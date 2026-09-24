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
from .access import ensure_staff_record_access
from datetime import timedelta


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


def ensure_restaurant_access(actor, restaurant_id):
    require_operations_manager(actor)
    if not actor.is_active or not actor.is_active_staff or actor.restaurant_id != restaurant_id:
        raise PermissionDenied("You cannot manage another restaurant.")


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
    from business_settings.services import resolve_settings
    branch = order.branch if order is not None else recipient.branch
    if not resolve_settings(restaurant, branch).get('notify_' + notification_type, True):
        return None
    notification, _ = StaffNotification.objects.get_or_create(
        recipient=recipient,
        event_key=event_key,
        defaults={
            "restaurant": restaurant,
            "branch_id": order.branch_id if order else recipient.branch_id,
            "order": order,
            "notification_type": notification_type,
            "title": title,
            "message": message,
        },
    )

    return notification


def _management_users(restaurant):
    return User.objects.filter(restaurant=restaurant, is_active=True, is_active_staff=True, role__in=MANAGEMENT_ROLES)


def _notify_management(
    *,
    restaurant,
    notification_type,
    title,
    message,
    event_key,
    order=None,
):
    users = _management_users(restaurant)
    if order is not None:
        users = users.filter(Q(role__in=["owner", "admin"]) | Q(branch_id=order.branch_id))
    for user in users:
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
            branch_id=order.branch_id,
            check_in__gte=timezone.now() - timedelta(days=1),
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

    ensure_staff_record_access(actor, attendance.restaurant_id, attendance.branch_id)
    if not attendance.employee.user.is_active or not attendance.employee.user.is_active_staff:
        raise ValidationError("This employee is inactive.")
    if attendance.employee.user.branch_id != attendance.branch_id:
        raise ValidationError("Attendance does not match the employee branch.")
    if attendance.work_date not in {local_work_date() - timedelta(days=1), local_work_date(), local_work_date() + timedelta(days=1)}:
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
        branch_id=attendance.branch_id,
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
    branch_id=None,
):
    table = (
        Table.objects
        .select_for_update()
        .filter(
            pk=table_id,
            restaurant_id=restaurant_id,
            branch_id=branch_id,
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

    ensure_staff_record_access(actor, attendance.restaurant_id, attendance.branch_id)
    if not attendance.employee.user.is_active or not attendance.employee.user.is_active_staff:
        raise ValidationError("This employee is inactive.")
    if attendance.employee.user.branch_id != attendance.branch_id:
        raise ValidationError("Attendance does not match the employee branch.")
    if attendance.work_date not in {local_work_date() - timedelta(days=1), local_work_date(), local_work_date() + timedelta(days=1)}:
        raise ValidationError(
            "Tasks can only be assigned to today's attendance."
        )

    if attendance.check_out is not None:
        raise ValidationError(
            "This employee has already checked out."
        )

    title = title.strip()
    instructions = instructions.strip()

    if not title or len(title) > 200:
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
                branch_id=attendance.branch_id,
            )
            .exclude(status__in=["COMPLETED", "CANCELLED"])
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
            branch_id=attendance.branch_id,
        )

    if order and table and order.table_id != table.pk:
        raise ValidationError("The task order and table must match.")
    task = StaffTask.objects.create(
        restaurant=attendance.restaurant,
        work_date=attendance.work_date,
        branch_id=attendance.branch_id,
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

    ensure_staff_record_access(actor, task.restaurant_id, task.branch_id)
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
            branch_id=order.branch_id,
            attendance__check_out__isnull=True,
            waiter__user__is_active=True,
            waiter__user__is_active_staff=True,
            work_date__in=[work_date, work_date - timedelta(days=1)],
            is_active=True,
        )
        .first()
    )


def ensure_order_staff_service(order, waiter_user=None):
    order_taken_by = None
    if waiter_user is not None and hasattr(waiter_user, "employee_profile"):
        order_taken_by = waiter_user.employee_profile

    try:
        service = order.staff_service
        if order_taken_by and service.order_taken_by_id is None:
            service.order_taken_by = order_taken_by
            service.save(update_fields=["order_taken_by"])
        return service
    except OrderStaffService.DoesNotExist:
        assignment = _find_table_assignment(order)
        waiter = assignment.waiter if assignment else None

        # If no daily table assignment, check if an order in this table session has an assigned waiter
        if waiter is None and order.table_id:
            session = getattr(order, "table_session", None)
            if session:
                existing_service = (
                    OrderStaffService.objects
                    .filter(
                        order__table_session=session,
                    )
                    .exclude(order=order)
                    .select_related("waiter", "table_assignment")
                    .order_by("-assigned_at")
                    .first()
                )
            else:
                from orders.services import get_live_order_cutoff
                cutoff = get_live_order_cutoff()
                existing_service = (
                    OrderStaffService.objects
                    .filter(
                        order__table=order.table,
                        order__created_at__gte=cutoff,
                    )
                    .exclude(order=order)
                    .select_related("waiter", "table_assignment")
                    .order_by("-assigned_at")
                    .first()
                )
            if existing_service:
                waiter = existing_service.waiter
                assignment = existing_service.table_assignment

        # If waiter is still None and waiter_user is provided
        if waiter is None and order_taken_by is not None:
            waiter = order_taken_by

        if waiter is None:
            return None
        if (not waiter.user.is_active or not waiter.user.is_active_staff
                or waiter.user.role != "waiter" or waiter.user.restaurant_id != order.restaurant_id
                or waiter.user.branch_id != order.branch_id):
            return None

        service, _ = OrderStaffService.objects.get_or_create(
            order=order,
            defaults={
                "waiter": waiter,
                "table_assignment": assignment,
                "order_taken_by": order_taken_by,
                "ready_at": order.status_changed_at if order.status == "READY" else None,
            },
        )
        if service and order_taken_by and service.order_taken_by_id is None:
            service.order_taken_by = order_taken_by
            service.save(update_fields=["order_taken_by"])

        return service


@transaction.atomic
def register_new_order(order_id, waiter_user=None):
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

    service = ensure_order_staff_service(order, waiter_user=waiter_user)

    if service is not None:
        is_qr = (order.table_id is not None and (waiter_user is None or getattr(service, "order_taken_by_id", None) is None))
        if is_qr:
            notif_title = f"New QR Order #{order.pk}"
            notif_message = f"{table_label} placed a customer QR order. You are the assigned waiter."
        else:
            staff_name = waiter_user.get_full_name() or waiter_user.username if waiter_user else "Staff"
            notif_title = f"New Order #{order.pk}"
            notif_message = f"{table_label} order entered by {staff_name}. You are the assigned waiter."

        _create_notification(
            restaurant=order.restaurant,
            recipient=service.waiter.user,
            order=order,
            notification_type="new_order",
            title=notif_title,
            message=notif_message,
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
        # Check if table was reassigned to a different waiter today
        current_assignment = _find_table_assignment(order)
        if current_assignment and current_assignment.waiter_id:
            if service is not None and service.waiter_id != current_assignment.waiter_id:
                service.handover_by = service.waiter
                service.waiter = current_assignment.waiter
                service.table_assignment = current_assignment
                service.save(update_fields=["waiter", "table_assignment", "handover_by"])
            elif service is None:
                service = OrderStaffService.objects.create(
                    order=order,
                    waiter=current_assignment.waiter,
                    table_assignment=current_assignment,
                    ready_at=order.status_changed_at or timezone.now(),
                )

        if service is not None:
            if service.ready_at is None:
                service.ready_at = order.status_changed_at or timezone.now()
                service.save(update_fields=["ready_at"])
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
        if service is not None:
            fields_to_update = []
            if service.served_at is None:
                service.served_at = timezone.now()
                fields_to_update.append("served_at")
            if service.served_by is None:
                service.served_by = service.waiter
                fields_to_update.append("served_by")
            if fields_to_update:
                service.save(update_fields=fields_to_update)

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


@transaction.atomic
def claim_order_service(order_id, waiter_user):
    """Allow a waiter to claim an unassigned order or handover order service."""
    order = (
        Order.objects
        .select_for_update(of=("self",))
        .select_related("restaurant", "table", "table_session")
        .get(pk=order_id)
    )

    ensure_staff_record_access(waiter_user, order.restaurant_id, order.branch_id)
    if waiter_user.role != "waiter":
        raise ValidationError("Only waiters can be assigned order service.")
    if order.status in {"SERVED", "COMPLETED", "CANCELLED"}:
        raise ValidationError("Service history cannot be reassigned after serving or closure.")
    if not hasattr(waiter_user, "employee_profile"):
        raise ValidationError("Only registered staff can claim orders.")

    waiter_profile = waiter_user.employee_profile

    if waiter_user.restaurant_id and waiter_user.restaurant_id != order.restaurant_id:
        raise PermissionDenied("You cannot claim orders from another restaurant.")

    service, created = OrderStaffService.objects.get_or_create(
        order=order,
        defaults={
            "waiter": waiter_profile,
            "ready_at": order.status_changed_at if order.status == "READY" else None,
        },
    )

    if not created:
        if service.waiter_id != waiter_profile.pk:
            service.waiter = waiter_profile
            service.table_assignment = None
            service.assigned_at = timezone.now()
            service.save(update_fields=["waiter", "table_assignment", "assigned_at"])

    # If the order is part of an open TableSession, ensure other unassigned active orders in the session are claimed as well
    if order.table_session:
        session_orders = Order.objects.filter(
            table_session=order.table_session
        ).exclude(pk=order.pk).exclude(status__in=["SERVED", "COMPLETED", "CANCELLED"])

        for s_order in session_orders:
            try:
                s_service = s_order.staff_service
                if s_service.waiter_id != waiter_profile.pk:
                    s_service.waiter = waiter_profile
                    s_service.table_assignment = None
                    s_service.assigned_at = timezone.now()
                    s_service.save(update_fields=["waiter", "table_assignment", "assigned_at"])
            except OrderStaffService.DoesNotExist:
                OrderStaffService.objects.create(
                    order=s_order,
                    waiter=waiter_profile,
                    ready_at=s_order.status_changed_at if s_order.status == "READY" else None,
                )

    return service
