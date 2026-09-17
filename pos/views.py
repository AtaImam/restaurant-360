import json

from decimal import (
    Decimal,
    InvalidOperation,
    ROUND_HALF_UP,
)

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from inventory.services import reserve_stock_for_order
from menu.models import Category, MenuItem
from orders.models import Order, OrderItem
from restaurant.models import Restaurant, Table
from staff.operations import register_new_order


# ============================================================
# HELPERS
# ============================================================

MONEY_PLACES = Decimal("0.01")


def money(value):
    return Decimal(
        value
    ).quantize(
        MONEY_PLACES,
        rounding=ROUND_HALF_UP,
    )


def validation_error_message(error):
    messages = getattr(
        error,
        "messages",
        None,
    )

    if messages:
        return " ".join(
            str(message)
            for message in messages
        )

    return str(error)


# ============================================================
# POS DASHBOARD
# ============================================================

def pos_dashboard(request):

    restaurant = (
        getattr(request.user, "restaurant", None) or Restaurant.objects.first()
    )

    categories = Category.objects.none()
    menu_items = MenuItem.objects.none()
    tables = Table.objects.none()

    if restaurant:

        categories = (
            Category.objects.filter(
                restaurant=restaurant
            )
            .order_by(
                "name"
            )
        )

        menu_items = (
            MenuItem.objects.filter(
                category__restaurant=
                    restaurant,

                is_available=True,
            )
            .select_related(
                "category"
            )
            .order_by(
                "category__name",
                "name",
            )
        )

        tables = (
            Table.objects.filter(
                restaurant=restaurant,
                is_active=True,
            )
            .order_by(
                "table_number"
            )
        )

    return render(
        request,
        "pos/dashboard.html",
        {
            "restaurant": restaurant,
            "categories": categories,
            "menu_items": menu_items,
            "tables": tables,
        },
    )


# ============================================================
# CREATE POS ORDER
# ============================================================

@require_POST
def create_pos_order(request):

    # ========================================================
    # PARSE REQUEST
    # ========================================================

    try:

        data = json.loads(
            request.body
        )
        if not isinstance(data, dict):
            raise ValueError("Request must be a JSON object.")

        order_type = (
            data.get(
                "order_type"
            )
        )

        table_id = (
            data.get(
                "table_id"
            )
        )

        cart_items = (
            data.get(
                "items",
                [],
            )
        )

        discount_amount = Decimal(
            str(
                data.get(
                    "discount_amount",
                    0,
                )
            )
        )

        service_percent = Decimal(
            str(
                data.get(
                    "service_percent",
                    0,
                )
            )
        )

        vat_percent = Decimal(
            str(
                data.get(
                    "vat_percent",
                    0,
                )
            )
        )
        if not all(value.is_finite() for value in (
            discount_amount, service_percent, vat_percent
        )):
            raise ValueError("Billing values must be finite numbers.")

    except (
        ValueError,
        TypeError,
        InvalidOperation,
        json.JSONDecodeError,
    ):

        return JsonResponse(
            {
                "success": False,
                "message":
                    "Invalid request data.",
            },
            status=400,
        )

    # ========================================================
    # BASIC VALIDATION
    # ========================================================

    if order_type not in [
        "DINE_IN",
        "TAKEAWAY",
    ]:

        return JsonResponse(
            {
                "success": False,
                "message":
                    "Invalid order type.",
            },
            status=400,
        )

    if (
        not isinstance(
            cart_items,
            list,
        )
        or
        not cart_items
    ):

        return JsonResponse(
            {
                "success": False,
                "message":
                    "Cart is empty.",
            },
            status=400,
        )

    restaurant = (
        getattr(request.user, "restaurant", None) or Restaurant.objects.first()
    )

    if not restaurant:

        return JsonResponse(
            {
                "success": False,
                "message":
                    "Restaurant not found.",
            },
            status=400,
        )

    # ========================================================
    # TABLE
    # ========================================================

    table = None

    if order_type == "DINE_IN":

        if not table_id:

            return JsonResponse(
                {
                    "success": False,
                    "message":
                        "Please select a table.",
                },
                status=400,
            )

        try:
            if not str(table_id).isascii() or not str(table_id).isdigit():
                raise ValueError
            table_id = int(
                table_id
            )

        except (
            TypeError,
            ValueError,
        ):

            return JsonResponse(
                {
                    "success": False,
                    "message":
                        "Invalid table.",
                },
                status=400,
            )

        table = (
            Table.objects.filter(
                id=table_id,
                restaurant=restaurant,
                is_active=True,
            )
            .first()
        )

        if not table:

            return JsonResponse(
                {
                    "success": False,
                    "message":
                        "Selected table is unavailable.",
                },
                status=400,
            )

    # ========================================================
    # SERVER-SIDE MENU / PRICE VALIDATION
    # ========================================================

    subtotal = Decimal(
        "0.00"
    )

    validated_items = []

    for cart_item in cart_items:

        if not isinstance(
            cart_item,
            dict,
        ):
            return JsonResponse(
                {
                    "success": False,
                    "message":
                        "Invalid cart item.",
                },
                status=400,
            )

        try:
            item_id_text = str(cart_item.get("id", ""))
            quantity_text = str(cart_item.get("quantity", ""))
            if not all(value.isascii() and value.isdigit() for value in (
                item_id_text, quantity_text
            )):
                raise ValueError

            menu_item_id = int(
                cart_item.get(
                    "id"
                )
            )

            quantity = int(
                cart_item.get(
                    "quantity",
                    0,
                )
            )

        except (
            TypeError,
            ValueError,
        ):

            return JsonResponse(
                {
                    "success": False,
                    "message":
                        "Invalid menu item data.",
                },
                status=400,
            )

        if not 1 <= quantity <= 2147483647:
            return JsonResponse(
                {"success": False, "message": "Every quantity must be a positive whole number."},
                status=400,
            )

        menu_item = (
            MenuItem.objects.filter(
                id=menu_item_id,

                category__restaurant=
                    restaurant,

                is_available=True,
            )
            .first()
        )

        if not menu_item:

            return JsonResponse(
                {
                    "success": False,

                    "message":
                        (
                            "One or more menu "
                            "items are unavailable."
                        ),
                },
                status=400,
            )

        line_total = (
            menu_item.price *
            quantity
        )

        subtotal += line_total
        if subtotal > Decimal("99999999.99"):
            return JsonResponse(
                {"success": False, "message": "This order exceeds the supported total amount."},
                status=400,
            )

        validated_items.append(
            {
                "menu_item":
                    menu_item,

                "quantity":
                    quantity,

                "price":
                    menu_item.price,
            }
        )

    if not validated_items:

        return JsonResponse(
            {
                "success": False,
                "message":
                    "No valid items found.",
            },
            status=400,
        )

    # ========================================================
    # BILLING VALIDATION
    # ========================================================

    subtotal = money(
        subtotal
    )

    if discount_amount < 0:
        discount_amount = (
            Decimal("0.00")
        )

    if discount_amount > subtotal:
        discount_amount = subtotal

    discount_amount = money(
        discount_amount
    )

    if service_percent < 0:
        service_percent = Decimal(
            "0.00"
        )

    if service_percent > 100:
        service_percent = Decimal(
            "100.00"
        )

    if vat_percent < 0:
        vat_percent = Decimal(
            "0.00"
        )

    if vat_percent > 100:
        vat_percent = Decimal(
            "100.00"
        )

    after_discount = (
        subtotal -
        discount_amount
    )

    service_charge = money(
        (
            after_discount *
            service_percent
        )
        /
        Decimal("100")
    )

    taxable_amount = (
        after_discount +
        service_charge
    )

    vat_amount = money(
        (
            taxable_amount *
            vat_percent
        )
        /
        Decimal("100")
    )

    grand_total = money(
        taxable_amount +
        vat_amount
    )
    if grand_total > Decimal("99999999.99"):
        return JsonResponse(
            {"success": False, "message": "This order exceeds the supported total amount."},
            status=400,
        )

    # ========================================================
    # ORDER + ITEMS + INVENTORY RESERVATION
    #
    # ALL THREE ARE ONE DATABASE TRANSACTION.
    # ========================================================

    try:

        with transaction.atomic():

            order = Order.objects.create(
                restaurant=restaurant,
                table=table,
                order_type=order_type,
                status="NEW",

                subtotal=
                    subtotal,

                discount_amount=
                    discount_amount,

                service_charge=
                    service_charge,

                vat_amount=
                    vat_amount,

                total_amount=
                    grand_total,

                payment_status=
                    "UNPAID",
            )

            OrderItem.objects.bulk_create(
                [
                    OrderItem(
                        order=order,

                        menu_item=(
                            item[
                                "menu_item"
                            ]
                        ),

                        quantity=(
                            item[
                                "quantity"
                            ]
                        ),

                        price=(
                            item[
                                "price"
                            ]
                        ),
                    )

                    for item
                    in validated_items
                ]
            )

            # -----------------------------------------------
            # RESERVE RECIPE INGREDIENTS
            # -----------------------------------------------

            reservations = (
                reserve_stock_for_order(
                    order
                )
            )
            transaction.on_commit(
                lambda order_id=order.pk: register_new_order(
                    order_id
                )
            )

    except ValidationError as error:

        return JsonResponse(
            {
                "success": False,

                "message":
                    validation_error_message(
                        error
                    ),

                "error_type":
                    "STOCK_UNAVAILABLE",
            },
            status=409,
        )

    # ========================================================
    # SUCCESS
    # ========================================================

    return JsonResponse(
        {
            "success": True,

            "message":
                "Order created and inventory reserved.",

            "order_id":
                order.id,

            "inventory_reserved":
                True,

            "reserved_ingredients":
                len(
                    reservations
                ),

            "subtotal":
                str(
                    order.subtotal
                ),

            "discount_amount":
                str(
                    order.discount_amount
                ),

            "service_charge":
                str(
                    order.service_charge
                ),

            "vat_amount":
                str(
                    order.vat_amount
                ),

            "total_amount":
                str(
                    order.total_amount
                ),
        }
    )
