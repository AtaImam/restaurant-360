"""Order workflow shared by kitchen and restaurant staff actions."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from inventory.models import StockReservation, StockTransaction
from inventory.services import consume_order_reservations, reserve_stock_for_order
from orders.models import Order
from restaurant.models import Restaurant


ORDER_TRANSITIONS = {
    "NEW": "ACCEPTED",
    "ACCEPTED": "PREPARING",
    "PREPARING": "READY",
    "READY": "SERVED",
    "SERVED": "COMPLETED",
}
INVENTORY_CONSUMED_STATUSES = frozenset({"PREPARING", "READY", "SERVED", "COMPLETED"})
LIVE_STATUSES = ["NEW", "ACCEPTED", "PREPARING", "READY"]
STALE_AFTER_HOURS = 12


def get_live_order_cutoff(now=None):
    """Orders created before this cutoff are considered stale and not part of the live service."""
    if now is None:
        now = timezone.now()
    start_of_today = timezone.localtime(now).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(start_of_today, now - timezone.timedelta(hours=STALE_AFTER_HOURS))


def remember_customer_order(request, order):
    """Store authorized order and session IDs in the customer's browser session."""
    if not hasattr(request, "session"):
        return
    order_ids = set(request.session.get("customer_order_ids", []))
    order_ids.add(order.pk)
    request.session["customer_order_ids"] = list(order_ids)[-100:]

    if order.table_session_id:
        session_ids = set(request.session.get("customer_table_sessions", []))
        session_ids.add(order.table_session_id)
        request.session["customer_table_sessions"] = list(session_ids)[-50:]
    request.session.modified = True


def can_customer_access_order(request, order):
    """Check if the current browser session has authorization for this order."""
    if not hasattr(request, "session"):
        return False
    if order.pk in request.session.get("customer_order_ids", []):
        return True
    if order.table_session_id and order.table_session_id in request.session.get("customer_table_sessions", []):
        return True
    return False


@transaction.atomic
def release_settled_table_session(session_id):
    """Release finished service independently of its cleaning acknowledgement."""
    from orders.models import TableSession
    from restaurant.models import Table

    table_id = TableSession.objects.values_list("table_id", flat=True).get(pk=session_id)
    table = Table.objects.select_for_update().get(pk=table_id)
    session = TableSession.objects.select_for_update().get(pk=session_id)
    if session.status != TableSession.STATUS_OPEN:
        return
    orders = session.orders.exclude(status="CANCELLED")
    if not session.orders.exists():
        return
    # Session must remain open until all non-cancelled orders are completed
    if orders.exclude(status="COMPLETED").exists():
        return
    session.status = TableSession.STATUS_CLOSED
    session.closed_at = timezone.now()
    session.clean_needed = orders.exists()
    session.save(update_fields=["status", "closed_at", "clean_needed"])
    if not table.sessions.filter(status=TableSession.STATUS_OPEN, opened_at__gte=get_live_order_cutoff()).exists():
        Table.objects.filter(pk=table.pk).exclude(
            status__in=[Table.STATUS_RESERVED, Table.STATUS_OUT_OF_SERVICE]
        ).update(status=Table.STATUS_AVAILABLE)


def orders_for_user(user):
    """Allow public workflow access and preserve signed-in staff assignment.

    Owners and admins see all orders for their restaurant.
    Staff users with an assigned branch only see that branch's orders.
    """
    orders = Order.objects.all()
    if not user.is_authenticated:
        return orders
    if user.role in ("admin", "owner") or user.is_superuser:
        restaurant_id = getattr(user, "restaurant_id", None)
        if restaurant_id is None:
            restaurant_id = Restaurant.objects.order_by("pk").values_list("pk", flat=True).first()
        return orders.filter(restaurant_id=restaurant_id)
    # Regular staff: scope to their restaurant first
    restaurant_id = getattr(user, "restaurant_id", None)
    if restaurant_id is None:
        restaurant_id = Restaurant.objects.order_by("pk").values_list("pk", flat=True).first()
    qs = orders.filter(restaurant_id=restaurant_id)
    # Further scope to their assigned branch if they have one
    branch_id = getattr(user, "branch_id", None)
    if branch_id:
        qs = qs.filter(branch_id=branch_id)
    return qs


def _ensure_early_reservations(order):
    reservations = order.stock_reservations.all()
    if reservations.filter(status=StockReservation.Status.CONSUMED).exists() or (
        order.stock_transactions.filter(
            transaction_type=StockTransaction.TransactionType.CONSUMPTION
        ).exists()
    ):
        raise ValidationError("This order already has inventory consumption; review its history before advancing it.")
    # Preserve the ingredient quantities reserved at checkout. Existing early
    # orders without reservations can still enter the corrected workflow.
    if not reservations.filter(status=StockReservation.Status.ACTIVE).exists():
        reserve_stock_for_order(order)


@transaction.atomic
def transition_order_status(order, new_status, *, allowed_targets=None):
    """Validate against the locked database status and apply inventory atomically.

    A repeated explicit target is idempotent. Callers must submit that target,
    rather than ask the endpoint to advance whatever state it happens to read.
    """
    locked_order = Order.objects.select_for_update().get(pk=order.pk)
    if new_status not in dict(Order.STATUS_CHOICES):
        raise ValidationError("Choose a valid order status.")
    if allowed_targets is not None and new_status not in allowed_targets:
        raise ValidationError("This action is not available in this workflow.")

    current_status = locked_order.status
    if new_status != current_status and ORDER_TRANSITIONS.get(current_status) != new_status:
        raise ValidationError(f"Cannot change an order from {current_status} to {new_status}.")

    # Payment guard: an order must be PAID before it can be marked COMPLETED.
    # This check runs inside the locked transaction so it cannot be bypassed.
    if new_status == "COMPLETED" and locked_order.payment_status != "PAID":
        raise ValidationError(
            "This order has not been paid. Please collect payment before completing the order."
        )

    if current_status in {"NEW", "ACCEPTED"}:
        _ensure_early_reservations(locked_order)

    if new_status in INVENTORY_CONSUMED_STATUSES:
        # The same transaction covers physical deduction, ledger, reservations,
        # and status. Later transitions verify the ledger and make no new
        # deduction after a valid PREPARING transition.
        consume_order_reservations(locked_order)

    if new_status != current_status:
        locked_order.status = new_status
        locked_order.status_changed_at = timezone.now()
        locked_order.save(update_fields=["status", "status_changed_at"])

        from staff.operations import handle_order_status_change

        handle_order_status_change(
            order_id=locked_order.pk,
            previous_status=current_status,
            new_status=new_status,
        )

    order.status = locked_order.status
    order.status_changed_at = locked_order.status_changed_at
    return locked_order


@transaction.atomic
def archive_stale_order(order, user=None, note="Archived stale order"):
    """Safely archive an unfinished/stale order without corrupting inventory or payment ledger.

    - If in NEW or ACCEPTED: unconsumed active stock reservations are released.
    - If in PREPARING, READY, or SERVED: stock was already consumed; preserves the ledger.
    - Frees the associated table if no other live orders exist for today's service.
    - Sets order.status = "CANCELLED".
    """
    locked_order = Order.objects.select_for_update().get(pk=order.pk)
    if locked_order.status in {"COMPLETED", "CANCELLED"}:
        order.status = locked_order.status
        return locked_order

    # Release unconsumed early reservations without touching physical stock
    if locked_order.status in {"NEW", "ACCEPTED"}:
        StockReservation.objects.filter(
            order=locked_order,
            status=StockReservation.Status.ACTIVE,
        ).update(status=StockReservation.Status.RELEASED)

    locked_order.status = Order.STATUS_CANCELLED
    locked_order.status_changed_at = timezone.now()
    locked_order.save(update_fields=["status", "status_changed_at"])

    # Session-backed orders release only their own service in Order.save().
    if locked_order.table_id and not locked_order.table_session_id:
        table = locked_order.table
        has_other_live = Order.objects.filter(
            table=table, created_at__gte=get_live_order_cutoff(),
        ).exclude(pk=locked_order.pk).exclude(status__in=["COMPLETED", "CANCELLED"]).exists()
        if not has_other_live:
            table.close_service()

    order.status = locked_order.status
    order.status_changed_at = locked_order.status_changed_at
    return locked_order


def record_order_payment(order, amount=None, payment_method="CASH", reference="", recorded_by=None, transaction_at=None, note=""):
    """
    Idempotently record payment for an order and create a PaymentTransaction ledger entry.
    Ensures that retrying or settling an already paid order does not create duplicate transactions.
    """
    from orders.models import PaymentTransaction

    with transaction.atomic():
        locked_order = Order.objects.select_for_update().get(pk=order.pk)

        # Check if a payment transaction already exists
        existing_payment = locked_order.transactions.filter(
            transaction_type=PaymentTransaction.TYPE_PAYMENT
        ).first()

        effective_amount = amount if amount is not None else locked_order.total_amount
        effective_time = transaction_at or timezone.now()

        if existing_payment:
            # Idempotent: order is already marked paid or has payment txn.
            if locked_order.payment_status != "PAID":
                locked_order.payment_status = "PAID"
                locked_order.paid_at = locked_order.paid_at or existing_payment.transaction_at
                locked_order.payment_method = payment_method or locked_order.payment_method or "CASH"
                locked_order.payment_reference = reference or locked_order.payment_reference
                locked_order.save(update_fields=["payment_status", "paid_at", "payment_method", "payment_reference"])
            order.payment_status = locked_order.payment_status
            order.paid_at = locked_order.paid_at
            order.payment_method = locked_order.payment_method
            order.payment_reference = locked_order.payment_reference
            return existing_payment

        from business_settings.services import resolve_settings, payment_choices
        enabled_methods = dict(payment_choices(resolve_settings(locked_order.restaurant, locked_order.branch)))
        if (payment_method or "CASH") not in enabled_methods:
            raise ValidationError("This payment method is disabled for this branch.")

        txn = PaymentTransaction.objects.create(
            restaurant_id=locked_order.restaurant_id,
            order=locked_order,
            transaction_type=PaymentTransaction.TYPE_PAYMENT,
            amount=effective_amount,
            payment_method=payment_method or "CASH",
            reference=reference or "",
            transaction_at=effective_time,
            recorded_by=recorded_by,
            note=note or "",
        )

        locked_order.payment_status = "PAID"
        locked_order.paid_at = effective_time
        locked_order.payment_method = payment_method or "CASH"
        locked_order.payment_reference = reference or ""
        locked_order.save(update_fields=["payment_status", "paid_at", "payment_method", "payment_reference"])

        if recorded_by and hasattr(locked_order, "staff_service"):
            service = locked_order.staff_service
            if service and service.payment_handled_by_id is None:
                service.payment_handled_by = recorded_by
                service.save(update_fields=["payment_handled_by"])

        order.payment_status = locked_order.payment_status
        order.paid_at = locked_order.paid_at
        order.payment_method = locked_order.payment_method
        order.payment_reference = locked_order.payment_reference
        return txn


def record_order_refund(order, amount, reason="", payment_method=None, recorded_by=None, transaction_at=None):
    """
    Record a refund transaction for an order without deleting original payment history.
    Reduces net collections and updates refund_amount.
    """
    from decimal import Decimal
    from orders.models import PaymentTransaction

    amount = Decimal(str(amount))
    if amount <= Decimal("0.00"):
        raise ValidationError("Refund amount must be greater than zero.")

    with transaction.atomic():
        locked_order = Order.objects.select_for_update().get(pk=order.pk)
        if locked_order.payment_status not in {"PAID", "REFUNDED"}:
            raise ValidationError("Cannot refund an unpaid order.")

        current_refunded = locked_order.refund_amount or Decimal("0.00")
        if current_refunded + amount > locked_order.total_amount:
            raise ValidationError(
                f"Total refund cannot exceed order total ({locked_order.total_amount}). "
                f"Already refunded: {current_refunded}."
            )

        effective_time = transaction_at or timezone.now()
        effective_method = payment_method or locked_order.payment_method or "CASH"

        txn = PaymentTransaction.objects.create(
            restaurant_id=locked_order.restaurant_id,
            order=locked_order,
            transaction_type=PaymentTransaction.TYPE_REFUND,
            amount=amount,
            payment_method=effective_method,
            reference=reason or "",
            transaction_at=effective_time,
            recorded_by=recorded_by,
            note=reason or "",
        )

        locked_order.refund_amount = current_refunded + amount
        if locked_order.refund_amount >= locked_order.total_amount:
            locked_order.payment_status = "REFUNDED"
        locked_order.save(update_fields=["refund_amount", "payment_status"])

        order.refund_amount = locked_order.refund_amount
        order.payment_status = locked_order.payment_status
        return txn
