from business_settings.services import resolve_settings, validate_new_order, payment_choices
import json

from decimal import (
    Decimal,
    InvalidOperation,
    ROUND_HALF_UP,
)

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Prefetch, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from inventory.services import (
    calculate_addon_available_portions,
    calculate_item_available_portions,
    reserve_stock_for_order,
)
from menu.addon_services import validate_item_addons
from menu.models import AddonGroup, AddonOption, Category, MenuItem
from orders.coupon_services import (
    apply_coupon_usage_atomic,
    calculate_order_pricing,
    validate_and_calculate_coupon,
)
from orders.models import Coupon, Order, OrderItem, OrderItemAddon
from orders.services import get_live_order_cutoff
from restaurant.branch_services import get_active_branch
from restaurant.models import Restaurant, Table
from staff.operations import register_new_order


VALID_PAYMENT_METHODS = {m for m, _ in Order.PAYMENT_METHOD_CHOICES}


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

@login_required(login_url='/auth/login/')
def pos_dashboard(request):

    restaurant = (
        getattr(request.user, "restaurant", None) or Restaurant.objects.first()
    )
    # Resolve active branch (staff → assigned branch; owner → session/main branch)
    active_branch = get_active_branch(request, restaurant) if restaurant else None

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

        menu_items_qs = (
            MenuItem.objects.filter(
                category__restaurant=
                    restaurant,

                is_available=True,
            )
            .select_related(
                "category"
            )
            .prefetch_related(
                Prefetch(
                    "addon_groups",
                    queryset=AddonGroup.objects.filter(is_active=True).prefetch_related(
                        Prefetch(
                            "options",
                            queryset=AddonOption.objects.filter(is_active=True)
                            .prefetch_related("ingredient_requirements__ingredient")
                            .order_by("display_order", "id"),
                        )
                    ).order_by("display_order", "id"),
                )
            )
            .order_by(
                "category__name",
                "name",
            )
        )
        menu_items = list(menu_items_qs)
        for item in menu_items:
            portions = calculate_item_available_portions(item)
            item.available_portions = portions
            item.is_sold_out = (portions is not None and portions < 1)
            groups = []
            for g in item.addon_groups.all():
                opts = [
                    {
                        "id": opt.id,
                        "name": opt.name,
                        "price": float(opt.price),
                        "is_sold_out": (calculate_addon_available_portions(opt) <= 0),
                    }
                    for opt in g.options.all()
                ]
                if opts:
                    groups.append({
                        "id": g.id,
                        "name": g.name,
                        "selection_type": g.selection_type,
                        "is_required": g.is_required,
                        "min_selection": g.min_selection,
                        "max_selection": g.max_selection,
                        "options": opts,
                    })
            item.addon_groups_json = json.dumps(groups)
            item.has_addons = len(groups) > 0

        # --- Branch-scoped table query ---
        table_qs = Table.objects.filter(
            restaurant=restaurant,
            is_active=True,
        )
        if active_branch:
            table_qs = table_qs.filter(branch=active_branch)
        tables = list(
            table_qs
            .select_related("floor")
            .order_by("floor__floor_number", "table_number")
        )
        live_cutoff = get_live_order_cutoff()
        table_ids = [t.id for t in tables]

        # Branch-scoped order queries using branch FK directly
        order_base_filter = {"restaurant": restaurant}
        if active_branch:
            order_base_filter["branch"] = active_branch

        active_orders = (
            Order.objects.filter(
                **order_base_filter,
                table_id__in=table_ids,
                created_at__gte=live_cutoff,
            )
            .filter(
                Q(table_session__status="OPEN")
                | Q(table_session__isnull=True)
            )
            .exclude(status__in=["COMPLETED", "CANCELLED"])
            .order_by("table_id", "-created_at")
        )
        orders_by_table = {}
        for o in active_orders:
            orders_by_table.setdefault(o.table_id, []).append(o)

        unpaid_tables = set(
            Order.objects.filter(
                **order_base_filter,
                table_id__in=table_ids,
                payment_status="UNPAID",
                created_at__gte=live_cutoff,
            )
            .filter(
                Q(table_session__status="OPEN")
                | Q(table_session__isnull=True)
            )
            .exclude(status__in=["COMPLETED", "CANCELLED"])
            .values_list("table_id", flat=True)
        )

        service_orders_by_table = set(
            Order.objects.filter(
                **order_base_filter,
                table_id__in=table_ids,
                created_at__gte=live_cutoff,
            )
            .filter(
                Q(table_session__status="OPEN")
                | Q(table_session__isnull=True)
            )
            .exclude(status="CANCELLED")
            .values_list("table_id", flat=True)
        )

        stale_order_tables = set(
            Order.objects.filter(
                **order_base_filter,
                table_id__in=table_ids,
                created_at__lt=live_cutoff,
            )
            .values_list("table_id", flat=True)
        )

        for t in tables:
            has_live_service = t.id in service_orders_by_table
            has_stale_orders = t.id in stale_order_tables
            if t.status == Table.STATUS_OCCUPIED and not has_live_service and has_stale_orders:
                # Left occupied from a previous day/session without any orders today: auto-heal to AVAILABLE
                t.close_service()

            t.current_active_orders = orders_by_table.get(t.id, [])
            t.active_orders_count = len(t.current_active_orders)
            t.has_unpaid_orders = t.id in unpaid_tables
            t.unpaid_balance = sum((o.total_amount for o in t.current_active_orders if o.payment_status != "PAID"), Decimal("0.00"))
            t.latest_order_id = t.current_active_orders[0].id if t.current_active_orders else None
            t.is_currently_occupied = (t.status == Table.STATUS_OCCUPIED and not (has_stale_orders and not has_live_service)) or bool(t.active_orders_count)

    is_waiter = getattr(request.user, "role", None) == "waiter"
    base_template = "waiter/base_waiter.html" if is_waiter else "dashboard/base_owner.html"
    unread_notifications_count = 0
    notifications = []
    if is_waiter:
        from staff.models import StaffNotification
        unread_notifications_count = StaffNotification.objects.filter(recipient=request.user, is_read=False).count()
        notifications = StaffNotification.objects.filter(recipient=request.user).order_by("-created_at")[:10]

    raw_table_id = request.GET.get("table_id")
    selected_table_id = ""
    if raw_table_id:
        try:
            selected_table_id = int(raw_table_id)
        except (ValueError, TypeError):
            selected_table_id = raw_table_id

    return render(
        request,
        "pos/dashboard.html",
        {
            "restaurant": restaurant,
            "operating_settings": resolve_settings(restaurant, active_branch),
            "active_branch": active_branch,
            "current_branch": active_branch,
            "categories": categories,
            "menu_items": menu_items,
            "tables": tables,
            "base_template": base_template,
            "selected_table_id": selected_table_id,
            "active_tab": "pos",
            "is_waiter": is_waiter,
            "waiter_user": request.user,
            "unread_notifications_count": unread_notifications_count,
            "notifications": notifications,
        },
    )


@require_POST
@login_required(login_url='/auth/login/')
def close_pos_table(request, table_id):
    restaurant = (
        getattr(request.user, "restaurant", None) or Restaurant.objects.first()
    )
    active_branch = get_active_branch(request, restaurant) if restaurant else None
    # Ensure the table belongs to the active branch (prevent cross-branch close)
    table_filter = {"pk": table_id, "restaurant": restaurant}
    if active_branch:
        table_filter["branch"] = active_branch
    table = get_object_or_404(Table, **table_filter)
    session = table.active_session
    live_cutoff = get_live_order_cutoff()
    if session:
        unpaid = session.orders.filter(
            payment_status="UNPAID",
            created_at__gte=live_cutoff,
        ).exclude(status__in=["COMPLETED", "CANCELLED"])
    else:
        unpaid = Order.objects.filter(
            table=table,
            payment_status="UNPAID",
            created_at__gte=live_cutoff,
        ).exclude(status__in=["COMPLETED", "CANCELLED"])
    if unpaid.exists():
        order_str = ", ".join(f"#{o.id}" for o in unpaid[:3])
        return JsonResponse(
            {
                "success": False,
                "message": f"Table {table.table_number} has unpaid orders ({order_str}). Please collect payment before closing service.",
            },
            status=400,
        )

    table.close_service()
    return JsonResponse(
        {
            "success": True,
            "message": f"Table {table.table_number} service closed and is now Available.",
            "table_id": table.id,
        }
    )


@require_POST
@login_required(login_url='/auth/login/')
def validate_pos_coupon(request):
    try:
        data = json.loads(request.body)
        if not isinstance(data, dict):
            raise ValueError
        coupon_code = str(data.get("coupon_code", "")).strip()
        cart_items = data.get("items", [])
        if not isinstance(cart_items, list) or not cart_items:
            return JsonResponse({"success": False, "message": "Cart is empty."}, status=400)
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"success": False, "message": "Invalid request payload."}, status=400)

    restaurant = getattr(request.user, "restaurant", None) or Restaurant.objects.first()
    if not restaurant:
        return JsonResponse({"success": False, "message": "Restaurant not found."}, status=400)
    active_branch = get_active_branch(request, restaurant)

    validated_items = []
    for cart_item in cart_items:
        if not isinstance(cart_item, dict):
            return JsonResponse({"success": False, "message": "Invalid cart item."}, status=400)
        try:
            menu_item_id = int(cart_item.get("id"))
            quantity = int(cart_item.get("quantity", 0))
        except (TypeError, ValueError):
            return JsonResponse({"success": False, "message": "Invalid menu item data."}, status=400)
        if quantity < 1:
            return JsonResponse({"success": False, "message": "Quantity must be at least 1."}, status=400)

        menu_item = MenuItem.objects.filter(
            id=menu_item_id, category__restaurant=restaurant, is_available=True
        ).first()
        if not menu_item:
            return JsonResponse({"success": False, "message": "One or more items are unavailable."}, status=400)

        addon_ids = cart_item.get("addon_ids", [])
        try:
            validated_addons, unit_addons_total = validate_item_addons(menu_item, addon_ids)
        except ValidationError as e:
            return JsonResponse({"success": False, "message": validation_error_message(e)}, status=400)

        line_unit_price = menu_item.price + unit_addons_total
        validated_items.append({
            "menu_item": menu_item,
            "quantity": quantity,
            "price": menu_item.price,
            "addons": validated_addons,
            "line_unit_price": line_unit_price,
            "subtotal": line_unit_price * quantity,
        })

    try:
        pricing = calculate_order_pricing(
            restaurant=restaurant,
            branch=active_branch,
            items_data=validated_items,
            coupon_code=coupon_code,
        )
    except ValidationError as err:
        return JsonResponse({"success": False, "message": validation_error_message(err)}, status=400)

    coupon = pricing["applied_coupon"]
    if not coupon:
        if coupon_code:
            return JsonResponse({"success": False, "message": f"Coupon code '{coupon_code}' did not apply."}, status=400)
        return JsonResponse({
            "success": True,
            "has_coupon": False,
            "discount_amount": "0.00",
            "message": "No coupon applied.",
        })

    return JsonResponse({
        "success": True,
        "has_coupon": True,
        "coupon_id": coupon.id,
        "coupon_code": coupon.code,
        "coupon_name": coupon.name,
        "discount_type": coupon.discount_type,
        "discount_value": str(coupon.discount_value),
        "discount_amount": str(pricing["discount_amount"]),
        "subtotal": str(pricing["subtotal"]),
        "after_discount": str(pricing["after_discount"]),
        "message": f"Coupon '{coupon.name}' applied (−৳{pricing['discount_amount']}).",
    })


# ============================================================
# CREATE POS ORDER
# ============================================================

@require_POST
@login_required(login_url='/auth/login/')
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

        coupon_code = str(data.get("coupon_code", "")).strip()

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

        # Payment timing: PAY_NOW or PAY_LATER (default).
        payment_timing = str(data.get("payment_timing", "PAY_LATER")).strip().upper()
        if payment_timing not in {"PAY_NOW", "PAY_LATER"}:
            payment_timing = "PAY_LATER"

        payment_method = str(data.get("payment_method", "")).strip().upper()
        payment_reference = str(data.get("payment_reference", "")).strip()

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

    # PAY_NOW requires a valid payment method up front.
    if payment_timing == "PAY_NOW" and payment_method not in VALID_PAYMENT_METHODS:
        return JsonResponse(
            {
                "success": False,
                "message": "Please select a payment method (Cash, Card, or Mobile Banking).",
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

    # Resolve active branch for all branch-scoped operations
    active_branch = get_active_branch(request, restaurant)
    operating_settings = resolve_settings(restaurant, active_branch)
    try:
        validate_new_order(operating_settings, order_type, payment_timing, payment_method)
    except ValidationError as error:
        return JsonResponse({"success": False, "message": validation_error_message(error)}, status=400)

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

        table_qs = Table.objects.filter(
            id=table_id,
            restaurant=restaurant,
            is_active=True,
        )
        # Enforce branch isolation: table must belong to the active branch
        if active_branch:
            table_qs = table_qs.filter(branch=active_branch)
        table = table_qs.first()

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

        addon_ids = cart_item.get("addon_ids", [])
        try:
            validated_addons, unit_addons_total = validate_item_addons(menu_item, addon_ids)
        except ValidationError as e:
            return JsonResponse(
                {"success": False, "message": validation_error_message(e)},
                status=400,
            )

        line_unit_price = menu_item.price + unit_addons_total
        line_total = (
            line_unit_price *
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

                "addons":
                    validated_addons,

                "line_unit_price":
                    line_unit_price,

                "subtotal":
                    line_total,
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
    # PRICING / DISCOUNT ENGINE
    # ========================================================

    # Stacking guard: coupon OR manual discount, never stack both
    if coupon_code and discount_amount > Decimal("0.00"):
        return JsonResponse(
            {
                "success": False,
                "message": "Cannot combine a coupon code with a manual discount.",
            },
            status=400,
        )

    try:
        pricing = calculate_order_pricing(
            restaurant=restaurant,
            items_data=validated_items,
            coupon_code=coupon_code,
            manual_discount=discount_amount,
            branch=active_branch,
            settings_values=operating_settings,
        )
    except ValidationError as err:
        return JsonResponse(
            {
                "success": False,
                "message": validation_error_message(err),
            },
            status=400,
        )

    if pricing["total_amount"] > Decimal("99999999.99"):
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

            # Atomically enforce coupon usage limit if coupon used
            if pricing["applied_coupon"]:
                apply_coupon_usage_atomic(pricing["applied_coupon"].id)

            order = Order.objects.create(
                restaurant=restaurant,
                branch=active_branch,
                table=table,
                order_type=order_type,
                status="NEW",

                settings_snapshot=pricing["settings_snapshot"],
                subtotal=pricing["subtotal"],
                discount_amount=pricing["discount_amount"],
                service_charge=pricing["service_charge"],
                vat_amount=pricing["vat_amount"],
                total_amount=pricing["total_amount"],

                applied_coupon=pricing["applied_coupon"],
                coupon_code_snapshot=pricing["coupon_code_snapshot"],
                coupon_name_snapshot=pricing["coupon_name_snapshot"],
                discount_type_snapshot=pricing["discount_type_snapshot"],
                discount_rate_snapshot=pricing["discount_rate_snapshot"],

                # PAY_NOW: mark PAID immediately inside the same atomic block.
                payment_status="PAID" if payment_timing == "PAY_NOW" else "UNPAID",
                payment_method=payment_method if payment_timing == "PAY_NOW" else "",
                payment_reference=payment_reference if payment_timing == "PAY_NOW" else "",
            )

            # Dine-in service makes the table occupied.
            if order_type == "DINE_IN" and table:
                table.mark_occupied()

            for item in pricing["items_data"]:
                order_item = OrderItem.objects.create(
                    order=order,
                    menu_item=item["menu_item"],
                    quantity=item["quantity"],
                    price=item["price"],
                    discount_amount=item.get("discount_amount", Decimal("0.00")),
                )
                for addon in item["addons"]:
                    OrderItemAddon.objects.create(
                        order_item=order_item,
                        addon_option=addon,
                        addon_group_name=addon.group.name if addon.group else "",
                        addon_name=addon.name,
                        price=addon.price,
                    )

            # -----------------------------------------------
            # RESERVE RECIPE INGREDIENTS
            # -----------------------------------------------

            reservations = reserve_stock_for_order(order)

            if payment_timing == "PAY_NOW":
                from orders.services import record_order_payment
                record_order_payment(
                    order,
                    amount=pricing["total_amount"],
                    payment_method=payment_method or "CASH",
                    reference=payment_reference or "",
                    recorded_by=request.user if request.user.is_authenticated else None,
                )

            # Kitchen notification:
            # PAY_NOW  → notify immediately (payment already confirmed).
            # PAY_LATER → also notify immediately; payment is collected later.
            waiter_user = request.user if getattr(request.user, "role", None) in {"waiter", "manager", "admin", "owner"} else None
            register_new_order(order.pk, waiter_user=waiter_user)

    except ValidationError as error:

        return JsonResponse(
            {
                "success": False,
                "message": validation_error_message(error),
                "error_type": "STOCK_UNAVAILABLE",
            },
            status=409,
        )

    # ========================================================
    # SUCCESS — return redirect URL based on payment timing
    # ========================================================

    if payment_timing == "PAY_NOW":
        redirect_url = reverse("order_receipt", args=[order.id])
        message = "Order created, payment recorded, sent to kitchen."
    else:
        redirect_url = None
        message = "Order created and sent to kitchen (KOT)."

    return JsonResponse(
        {
            "success": True,
            "message": message,
            "order_id": order.id,
            "payment_timing": payment_timing,
            "redirect_url": redirect_url,
            "inventory_reserved": True,
            "reserved_ingredients": len(reservations),
            "subtotal": str(order.subtotal),
            "discount_amount": str(order.discount_amount),
            "service_charge": str(order.service_charge),
            "vat_amount": str(order.vat_amount),
            "total_amount": str(order.total_amount),
        }
    )
