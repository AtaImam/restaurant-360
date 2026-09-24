from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from guests.services import bill_feedback_context
from business_settings.services import resolve_settings, payment_choices
from django.views.decorators.cache import never_cache
from orders.models import Order
from orders.services import get_live_order_cutoff, orders_for_user
from staff.operations import register_new_order


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_order(order_id):
    """Fetch order with related data needed for billing views."""
    return get_object_or_404(
        Order.objects.select_related("restaurant", "table")
        .prefetch_related("items__menu_item"),
        id=order_id,
    )


VALID_PAYMENT_METHODS = {m for m, _ in Order.PAYMENT_METHOD_CHOICES}


def _table_service_summary(order):
    """Compute table session financial summary if order is dine-in on a table."""
    if not (order.order_type == "DINE_IN" and order.table):
        return None

    session = order.table_session
    if session:
        session_orders = list(
            session.orders
            .exclude(status="CANCELLED")
            .prefetch_related("items__menu_item")
            .order_by("created_at")
        )
    else:
        live_cutoff = get_live_order_cutoff()
        session_orders = list(
            Order.objects.filter(
                table=order.table,
                created_at__gte=live_cutoff,
            )
            .exclude(status="CANCELLED")
            .prefetch_related("items__menu_item")
            .order_by("created_at")
        )

    if not session_orders:
        return None

    paid_orders = [o for o in session_orders if o.payment_status == "PAID"]
    unpaid_orders = [o for o in session_orders if o.payment_status != "PAID"]

    total_amount = sum((o.total_amount for o in session_orders), Decimal("0.00"))
    paid_amount = sum((o.total_amount for o in paid_orders), Decimal("0.00"))
    unpaid_amount = sum((o.total_amount for o in unpaid_orders), Decimal("0.00"))

    return {
        "table": order.table,
        "session": session,
        "session_orders": session_orders,
        "paid_orders": paid_orders,
        "unpaid_orders": unpaid_orders,
        "total_amount": total_amount,
        "paid_amount": paid_amount,
        "unpaid_amount": unpaid_amount,
        "orders_count": len(session_orders),
        "is_fully_paid": unpaid_amount == Decimal("0.00"),
        "has_multiple_orders": len(session_orders) > 1,
    }


# ---------------------------------------------------------------------------
# Bill Preview
# ---------------------------------------------------------------------------

@never_cache
@login_required(login_url='/auth/login/')
def bill_preview(request, order_id):
    """Show itemised bill and payment method selector.

    Calculates bill at current TABLE SERVICE level for dine-in sessions so
    cashiers and guests see all current-service orders, paid vs unpaid amounts,
    and the combined outstanding balance.
    """
    order = _get_order(order_id)
    summary = _table_service_summary(order)

    error = None
    if request.method == "POST":
        payment_method = request.POST.get("payment_method", "").strip().upper()
        payment_reference = request.POST.get("payment_reference", "").strip()
        settle_table = request.POST.get("settle_scope") == "TABLE"
        next_url = request.POST.get("next", "")
        close_table = request.POST.get("close_table") == "1" or request.POST.get("free_table") == "1"

        error = _process_payment(
            order,
            payment_method,
            payment_reference=payment_reference,
            settle_table=settle_table,
            close_table=close_table,
            recorded_by=request.user if request.user.is_authenticated else None,
        )
        if error is None:
            if next_url == "pos":
                return redirect("pos_dashboard")
            from django.urls import reverse
            receipt_url = reverse("order_receipt", args=[order.id])
            if next_url:
                receipt_url += f"?next={next_url}"
            return redirect(receipt_url)

    return render(
        request,
        "orders/bill_preview.html",
        {
            "order": order,
            "summary": summary,
            "receipt_settings": order.settings_snapshot,
            **bill_feedback_context(request, order),
            "payment_method_choices": payment_choices(resolve_settings(order.restaurant, order.branch)),
            "error": error,
        },
    )


# ---------------------------------------------------------------------------
# Pay Order (JSON API)
# ---------------------------------------------------------------------------

@require_POST
@login_required(login_url='/auth/login/')
def pay_order(request, order_id):
    """JSON endpoint to record payment for an order or entire table service."""
    import json

    order = _get_order(order_id)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse(
            {"success": False, "message": "Invalid request body."},
            status=400,
        )

    payment_method = str(data.get("payment_method", "")).strip().upper()
    payment_reference = str(data.get("payment_reference", "")).strip()
    settle_table = bool(data.get("settle_table", False))
    notify_kitchen = bool(data.get("notify_kitchen", False))
    close_table = bool(data.get("close_table", False))

    error = _process_payment(
        order,
        payment_method,
        payment_reference=payment_reference,
        settle_table=settle_table,
        notify_kitchen=notify_kitchen,
        close_table=close_table,
        recorded_by=request.user if request.user.is_authenticated else None,
    )
    if error:
        return JsonResponse({"success": False, "message": error}, status=400)

    from django.urls import reverse

    return JsonResponse(
        {
            "success": True,
            "order_id": order.id,
            "receipt_url": reverse("order_receipt", args=[order.id]),
        }
    )


# ---------------------------------------------------------------------------
# Order Receipt
# ---------------------------------------------------------------------------

@never_cache
def order_receipt(request, order_id):
    """Print-friendly receipt shown after payment."""
    from orders.services import can_customer_access_order

    order = _get_order(order_id)
    user = request.user
    if not user.is_authenticated and not can_customer_access_order(request, order):
        return redirect(f"/auth/login/?next={request.path}")

    summary = _table_service_summary(order)

    can_free_table = False
    if order.table and order.table.is_occupied:
        session = order.table_session
        if session:
            has_unpaid = (
                session.orders
                .filter(payment_status="UNPAID")
                .exclude(status__in=["COMPLETED", "CANCELLED"])
                .exists()
            )
        else:
            live_cutoff = get_live_order_cutoff()
            has_unpaid = (
                Order.objects.filter(
                    table=order.table,
                    payment_status="UNPAID",
                    created_at__gte=live_cutoff,
                )
                .exclude(status__in=["COMPLETED", "CANCELLED"])
                .exists()
            )
        can_free_table = not has_unpaid

    return render(
        request,
        "orders/receipt.html",
        {
            "order": order,
            "summary": summary,
            "receipt_settings": order.settings_snapshot,
            **bill_feedback_context(request, order),
            "can_free_table": can_free_table,
        },
    )


# ---------------------------------------------------------------------------
# Internal payment processor (shared by bill_preview POST + pay_order)
# ---------------------------------------------------------------------------

def _process_payment(
    order,
    payment_method,
    *,
    payment_reference="",
    settle_table=False,
    notify_kitchen=False,
    close_table=False,
    recorded_by=None,
):
    """Atomically mark order(s) PAID and optionally trigger kitchen notification.

    If settle_table is True, marks all valid unpaid orders in the table's active
    service session as PAID with the selected payment method and reference.
    Excludes cancelled, already-paid, and fully-refunded orders.
    Any SERVED orders among them are transitioned to COMPLETED.
    A table is freed only if all current-service orders on it are fully settled.
    """
    if payment_method not in VALID_PAYMENT_METHODS:
        return "Please select a valid payment method (Cash, Card, or Mobile Banking)."

    try:
        with transaction.atomic():
            from orders.services import record_order_payment, transition_order_status

            live_cutoff = get_live_order_cutoff()
            session = order.table_session

            if settle_table and order.table and order.order_type == "DINE_IN":
                if session:
                    table_unpaid = list(
                        session.orders.select_for_update()
                        .filter(payment_status="UNPAID")
                        .exclude(status__in=["COMPLETED", "CANCELLED"])
                    )
                else:
                    table_unpaid = list(
                        Order.objects.select_for_update()
                        .filter(
                            table=order.table,
                            payment_status="UNPAID",
                            created_at__gte=live_cutoff,
                        )
                        .exclude(status__in=["COMPLETED", "CANCELLED"])
                    )

                # Filter out any fully refunded orders
                table_unpaid = [
                    item for item in table_unpaid
                    if (item.refund_amount or Decimal("0.00")) < item.total_amount
                ]

                table_num = order.table.table_number if order.table else "?"
                session_tag = f"Session #{session.id}" if session else "live service"
                for item in table_unpaid:
                    record_order_payment(
                        item,
                        payment_method=payment_method,
                        reference=payment_reference,
                        recorded_by=recorded_by,
                        note=f"Table settlement for Table {table_num} ({session_tag})",
                    )

                    if item.status == "SERVED":
                        transition_order_status(item, "COMPLETED")

                # Also ensure the primary order reference is updated if it was unpaid
                locked = Order.objects.select_for_update().get(pk=order.pk)
                if locked.payment_status != "PAID" and locked.status not in ["COMPLETED", "CANCELLED"]:
                    record_order_payment(
                        locked,
                        payment_method=payment_method,
                        reference=payment_reference,
                        recorded_by=recorded_by,
                        note=f"Table settlement for Table {table_num} ({session_tag})",
                    )
                    if locked.status == "SERVED":
                        transition_order_status(locked, "COMPLETED")

                # If there are any other SERVED orders that were already paid (e.g. earlier PAY_NOW orders),
                # complete them as part of final table settlement
                if session:
                    served_paid_orders = list(
                        session.orders.select_for_update()
                        .filter(payment_status="PAID", status="SERVED")
                    )
                    for item in served_paid_orders:
                        transition_order_status(item, "COMPLETED")
                else:
                    served_paid_orders = list(
                        Order.objects.select_for_update()
                        .filter(
                            table=order.table,
                            payment_status="PAID",
                            status="SERVED",
                            created_at__gte=live_cutoff,
                        )
                    )
                    for item in served_paid_orders:
                        transition_order_status(item, "COMPLETED")


            else:
                locked = Order.objects.select_for_update().get(pk=order.pk)

                if locked.payment_status == "PAID":
                    if locked.status == "SERVED":
                        transition_order_status(locked, "COMPLETED")

                    return None

                if locked.status in {"COMPLETED", "CANCELLED"}:
                    return "This order is already completed or cancelled."

                record_order_payment(
                    locked,
                    payment_method=payment_method,
                    reference=payment_reference,
                    recorded_by=recorded_by,
                )

                # Rule: Collecting payment on a SERVED order allows/performs COMPLETED.
                if locked.status == "SERVED":
                    transition_order_status(locked, "COMPLETED")

                if notify_kitchen:
                    transaction.on_commit(
                        lambda order_id=locked.pk: register_new_order(order_id)
                    )

    except ValidationError as error:
        return " ".join(error.messages)
    except Exception as exc:
        return f"Payment could not be recorded: {exc}"

    # Refresh the in-memory object so callers see the updated fields.
    order.refresh_from_db()
    return None
