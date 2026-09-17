"""Order workflow shared by kitchen and restaurant staff actions."""

from django.core.exceptions import ValidationError
from django.db import transaction

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


def orders_for_user(user):
    """Allow public workflow access and preserve signed-in staff assignment."""
    orders = Order.objects.all()
    if not user.is_authenticated:
        return orders
    if user.role == "admin":
        return orders
    restaurant_id = user.restaurant_id
    if restaurant_id is None:
        restaurant_id = Restaurant.objects.order_by("pk").values_list("pk", flat=True).first()
    return orders.filter(restaurant_id=restaurant_id)


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

    if current_status in {"NEW", "ACCEPTED"}:
        _ensure_early_reservations(locked_order)

    if new_status in INVENTORY_CONSUMED_STATUSES:
        # The same transaction covers physical deduction, ledger, reservations,
        # and status. Later transitions verify the ledger and make no new
        # deduction after a valid PREPARING transition.
        consume_order_reservations(locked_order)

    if new_status != current_status:
        locked_order.status = new_status
        locked_order.save(update_fields=["status"])

        from staff.operations import handle_order_status_change

        handle_order_status_change(
            order_id=locked_order.pk,
            previous_status=current_status,
            new_status=new_status,
        )

    order.status = locked_order.status
    return locked_order