import math
from datetime import timedelta

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET

from orders.models import Order


# ============================================================
# CUSTOMER ORDER TRACKING
# ============================================================

STATUS_META = {

    "NEW": {
        "label": "Pending",
        "title": "Waiting for restaurant confirmation",
        "progress": 8,
        "stage_index": 0,
    },

    "ACCEPTED": {
        "label": "Order Accepted",
        "title": "Your order has been accepted",
        "progress": 25,
        "stage_index": 1,
    },

    "PREPARING": {
        "label": "Cooking",
        "title": "Your food is being prepared",
        "progress": 52,
        "stage_index": 2,
    },

    "READY": {
        "label": "Ready",
        "title": "Your food is ready",
        "progress": 75,
        "stage_index": 3,
    },

    "SERVED": {
        "label": "Served",
        "title": "Your order has been served",
        "progress": 90,
        "stage_index": 4,
    },

    "COMPLETED": {
        "label": "Completed",
        "title": "Order completed",
        "progress": 100,
        "stage_index": 5,
    },
}


# ============================================================
# ETA CALCULATION
# ============================================================

def estimate_order_minutes(order):

    lines = list(
        order.items
        .select_related(
            "menu_item",
            "menu_item__category",
        )
        .all()
    )

    if not lines:
        return 15

    longest_item_minutes = 0
    total_quantity = 0

    for line in lines:

        quantity = max(
            int(line.quantity),
            1,
        )

        total_quantity += quantity

        item = line.menu_item

        category_name = (
            item.category.name
            if item.category
            else ""
        ).strip().lower()

        item_type = getattr(
            item,
            "item_type",
            "NORMAL",
        )

        # ----------------------------------------------------
        # REALISTIC DEMO PREPARATION ESTIMATE
        # ----------------------------------------------------

        if item_type == "SET_MENU":

            base_minutes = 22

        elif (
            "biryani" in category_name
            or
            "kacchi" in category_name
            or
            "curry" in category_name
        ):

            base_minutes = 20

        elif (
            "kebab" in category_name
            or
            "grill" in category_name
        ):

            base_minutes = 18

        elif "chinese" in category_name:

            base_minutes = 16

        elif (
            "burger" in category_name
            or
            "pizza" in category_name
            or
            "snack" in category_name
            or
            "fast food" in category_name
        ):

            base_minutes = 14

        elif (
            "drink" in category_name
            or
            "beverage" in category_name
            or
            "dessert" in category_name
        ):

            base_minutes = 8

        else:

            base_minutes = 15

        line_minutes = (
            base_minutes
            +
            min(
                (quantity - 1) * 2,
                6,
            )
        )

        longest_item_minutes = max(
            longest_item_minutes,
            line_minutes,
        )

    # Several dishes may cook in parallel,
    # so we do NOT simply add every item's time.

    queue_buffer = min(
        max(
            total_quantity - 1,
            0,
        ) * 2,
        8,
    )

    estimated_minutes = (
        longest_item_minutes
        +
        queue_buffer
    )

    return max(
        10,
        min(
            estimated_minutes,
            40,
        ),
    )


# ============================================================
# REMAINING TIME
# ============================================================

def remaining_minutes(
    order,
    estimated_minutes,
):

    estimated_ready_at = (
        order.created_at
        +
        timedelta(
            minutes=estimated_minutes
        )
    )

    seconds_left = (
        estimated_ready_at
        -
        timezone.now()
    ).total_seconds()

    return max(
        0,
        math.ceil(
            seconds_left / 60
        ),
    )


# ============================================================
# CUSTOMER MESSAGE
# ============================================================

def get_customer_message(
    order,
    status,
    minutes_left,
):

    if status == "NEW":

        return (
            "We received your order. "
            "The restaurant has not accepted it yet."
        )

    if status == "ACCEPTED":

        if minutes_left > 0:

            return (
                "The restaurant accepted your order. "
                f"Approximate preparation time is "
                f"{minutes_left} minute"
                f"{'s' if minutes_left != 1 else ''}."
            )

        return (
            "The restaurant accepted your order. "
            "The kitchen will start shortly."
        )

    if status == "PREPARING":

        if minutes_left > 0:

            return (
                "Your food is being prepared. "
                f"Roughly {minutes_left} minute"
                f"{'s' if minutes_left != 1 else ''} "
                f"remaining, estimated from when you placed the order."
            )

        return (
            "Your food is still being prepared and is taking longer than estimated. "
            "This page will update when the kitchen marks it ready."
        )

    if status == "READY":

        if (
            order.order_type == "DINE_IN"
        ):

            return (
                "Your food is ready. "
                "It will be served to your table shortly."
            )

        return (
            "Your order is ready for pickup."
        )

    if status == "SERVED":

        return (
            "Your order has been served. "
            "Enjoy your meal!"
        )

    if status == "COMPLETED":

        return (
            "This order is complete. "
            "Thank you for ordering with us."
        )

    return (
        "Your order status has been updated."
    )


# ============================================================
# LIVE STATUS API
# ============================================================

def get_order_status_data(order):
    """Present the persisted status without advancing it based on elapsed time."""
    status = order.status

    meta = STATUS_META.get(
        status,
        {
            "label":
                status.replace(
                    "_",
                    " ",
                ).title(),

            "title":
                "Order status updated",

            "progress":
                5,

            "stage_index":
                0,
        },
    )

    estimated_minutes = (
        estimate_order_minutes(
            order
        )
    )

    # The schema has no accepted_at/preparing_at timestamps. Acceptance shows
    # a preparation estimate; cooking uses an explicitly approximate estimate
    # from placement and never claims that elapsed time proves readiness.
    minutes_left = (
        estimated_minutes if status == "ACCEPTED"
        else remaining_minutes(order, estimated_minutes)
    )

    show_eta = (
        status in {
            "ACCEPTED",
            "PREPARING",
        }
    )

    return {
            "order_id":
                order.id,

            "status":
                status,

            "status_label":
                meta["label"],

            "title":
                meta["title"],

            "message":
                get_customer_message(
                    order,
                    status,
                    minutes_left,
                ),

            "progress":
                meta["progress"],

            "stage_index":
                meta["stage_index"],

            "estimated_total_minutes":
                estimated_minutes,

            "remaining_minutes":
                (
                    minutes_left
                    if show_eta and minutes_left > 0
                    else None
                ),

            "is_terminal":
                status == "COMPLETED",

            "order_type":
                order.order_type,

            "table_number":
                (
                    order.table.table_number
                    if order.table
                    else None
                ),
            "eta_note": (
                "Approximate preparation time; kitchen timing may vary."
                if status == "ACCEPTED"
                else "Rough estimate from order placement; kitchen timing may vary."
            ),
        }


@require_GET
def order_status_api(request, order_id):
    order = get_object_or_404(
        Order.objects.select_related("restaurant", "table")
        .prefetch_related("items__menu_item__category"),
        id=order_id,
    )
    response = JsonResponse(get_order_status_data(order))

    response[
        "Cache-Control"
    ] = (
        "no-store, no-cache, "
        "must-revalidate, max-age=0"
    )

    return response
