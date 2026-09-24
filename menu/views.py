from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from inventory.services import (
    calculate_item_available_portions,
    reserve_stock_for_order,
)
from orders.coupon_services import (
    apply_coupon_usage_atomic,
    calculate_order_pricing,
)
from orders.models import Order, OrderItem, OrderItemAddon
from orders.services import remember_customer_order
from restaurant.models import Restaurant, Table
from staff.operations import register_new_order

from .addon_services import validate_item_addons
from .models import AddonGroup, AddonOption, Category, MenuItem
from .tracking_views import get_order_status_data
from guests.services import remember_qr_order, qr_feedback_url
from business_settings.services import resolve_settings, validate_new_order, payment_choices
from django.views.decorators.cache import never_cache


def _get_table_session_summary(table, current_cart_total=Decimal("0.00")):
    if not table:
        return None
    active_session = getattr(table, "active_session", None)
    if not active_session:
        return None
    existing_orders = list(
        active_session.orders
        .exclude(status="CANCELLED")
        .prefetch_related("items__menu_item")
        .order_by("created_at")
    )
    if not existing_orders:
        return None

    session_total = sum((o.total_amount for o in existing_orders), Decimal("0.00"))
    paid_total = sum((o.total_amount for o in existing_orders if o.payment_status == "PAID"), Decimal("0.00"))
    unpaid_total = sum((o.total_amount for o in existing_orders if o.payment_status != "PAID"), Decimal("0.00"))
    combined_total = session_total + current_cart_total
    combined_unpaid = unpaid_total + current_cart_total

    return {
        "session": active_session,
        "existing_orders": existing_orders,
        "orders_count": len(existing_orders),
        "session_total": session_total,
        "paid_total": paid_total,
        "unpaid_total": unpaid_total,
        "combined_total": combined_total,
        "combined_unpaid": combined_unpaid,
    }


# ============================================================
# HELPERS
# ============================================================

def get_cart_key(restaurant_id, table_id):
    return f"cart_{restaurant_id}_{table_id}"


def get_table_cart(
    request,
    restaurant_id,
    table_id,
):
    key = get_cart_key(
        restaurant_id,
        table_id,
    )

    return request.session.get(
        key,
        {},
    )


def save_table_cart(
    request,
    restaurant_id,
    table_id,
    cart,
):
    key = get_cart_key(
        restaurant_id,
        table_id,
    )

    request.session[key] = cart
    request.session.modified = True


def calculate_cart_total(cart):
    total = Decimal("0.00")

    for item in cart.values():
        if not isinstance(item, dict):
            continue
        try:
            price_val = item.get("unit_price") if "unit_price" in item else item.get("price", 0)
            price = Decimal(str(price_val))
            quantity = int(item.get("quantity", 0))
            if not price.is_finite() or price < 0 or quantity < 0:
                raise ValueError
        except (InvalidOperation, TypeError, ValueError, OverflowError):
            price = Decimal("0.00")
            quantity = 0

        subtotal = (
            price *
            quantity
        )

        item["subtotal"] = float(
            subtotal
        )

        total += subtotal

    return total


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
# CUSTOMER MENU
# ============================================================

def customer_menu(
    request,
    restaurant_id,
    table_id,
):
    restaurant = get_object_or_404(
        Restaurant,
        id=restaurant_id,
    )

    table = get_object_or_404(
        Table,
        id=table_id,
        restaurant=restaurant,
        is_active=True,
    )

    available_items = (
        MenuItem.objects.filter(
            is_available=True,
            category__restaurant=restaurant,
        )
        .select_related(
            "category"
        )
        .order_by(
            "name"
        )
    )

    categories = (
        Category.objects.filter(
            restaurant=restaurant
        )
        .prefetch_related(
            Prefetch(
                "items",
                queryset=available_items,
                to_attr="available_items",
            )
        )
        .order_by(
            "name"
        )
    )

    for cat in categories:
        for itm in getattr(cat, "available_items", []):
            portions = calculate_item_available_portions(itm)
            itm.available_portions = portions
            itm.is_sold_out = (portions is not None and portions < 1)

    cart = get_table_cart(
        request,
        restaurant_id,
        table_id,
    )

    cart_count = sum(
        int(
            item.get(
                "quantity",
                0,
            )
        )
        for item in cart.values()
    )

    return render(
        request,
        "customer/menu.html",
        {
            "restaurant": restaurant,
            "table": table,
            "categories": categories,
            "cart_count": cart_count,
        },
    )


# ============================================================
# ITEM DETAILS
# ============================================================

def item_detail(
    request,
    restaurant_id,
    table_id,
    item_id,
):
    restaurant = get_object_or_404(
        Restaurant,
        id=restaurant_id,
    )

    table = get_object_or_404(
        Table,
        id=table_id,
        restaurant=restaurant,
        is_active=True,
    )

    item = get_object_or_404(
        MenuItem.objects.select_related(
            "category"
        ),
        id=item_id,
        category__restaurant=restaurant,
        is_available=True,
    )

    item_portions = calculate_item_available_portions(item)
    item.available_portions = item_portions
    item.is_sold_out = (item_portions is not None and item_portions < 1)

    set_components = []
    key_ingredients = []
    allergens = set()

    # ========================================================
    # SET MENU
    # ========================================================

    if (
        item.item_type ==
        MenuItem.ItemType.SET_MENU
    ):

        set_components = list(
            item.set_components
            .select_related(
                "component",
                "component__category",
            )
            .order_by(
                "display_order",
                "id",
            )
        )

        for set_component in set_components:

            component_item = (
                set_component.component
            )

            recipe = getattr(
                component_item,
                "recipe",
                None,
            )

            if not recipe:
                continue

            recipe_ingredients = (
                recipe.recipe_ingredients
                .filter(
                    is_customer_visible=True
                )
                .select_related(
                    "ingredient"
                )
                .prefetch_related(
                    "ingredient__allergens"
                )
            )

            for recipe_ingredient in recipe_ingredients:

                ingredient = (
                    recipe_ingredient.ingredient
                )

                for allergen in (
                    ingredient
                    .allergens
                    .all()
                ):
                    allergens.add(
                        allergen.name
                    )

    # ========================================================
    # NORMAL FOOD
    # ========================================================

    else:

        recipe = getattr(
            item,
            "recipe",
            None,
        )

        if recipe:

            recipe_ingredients = (
                recipe.recipe_ingredients
                .filter(
                    is_customer_visible=True
                )
                .select_related(
                    "ingredient"
                )
                .prefetch_related(
                    "ingredient__allergens"
                )
                .order_by(
                    "id"
                )
            )

            for recipe_ingredient in recipe_ingredients:

                ingredient = (
                    recipe_ingredient.ingredient
                )

                key_ingredients.append(
                    ingredient.name
                )

                for allergen in (
                    ingredient
                    .allergens
                    .all()
                ):
                    allergens.add(
                        allergen.name
                    )

    cart = get_table_cart(
        request,
        restaurant_id,
        table_id,
    )

    cart_count = sum(
        int(
            cart_item.get(
                "quantity",
                0,
            )
        )
        for cart_item in cart.values()
    )

    addon_groups = list(
        item.addon_groups.filter(
            is_active=True,
            restaurant=restaurant,
        )
        .prefetch_related(
            Prefetch(
                "options",
                queryset=AddonOption.objects.filter(is_active=True)
                .prefetch_related("ingredient_requirements__ingredient")
                .order_by("display_order", "id"),
            )
        )
        .order_by("display_order", "id")
    )

    from inventory.services import calculate_addon_available_portions
    for group in addon_groups:
        has_checked = False
        for opt in group.options.all():
            opt.is_sold_out = (calculate_addon_available_portions(opt) <= 0)
            if group.selection_type == "SINGLE" and group.is_required and not opt.is_sold_out and not has_checked:
                opt.is_default_checked = True
                has_checked = True
            else:
                opt.is_default_checked = False

    return render(
        request,
        "customer/item_detail.html",
        {
            "restaurant": restaurant,
            "table": table,
            "item": item,
            "set_components": set_components,
            "key_ingredients": key_ingredients,
            "allergens": sorted(
                allergens
            ),
            "addon_groups": addon_groups,
            "cart_count": cart_count,
        },
    )


# ============================================================
# ADD TO CART
# ============================================================

@require_POST
def add_to_cart(
    request,
    restaurant_id,
    table_id,
    item_id,
):
    restaurant = get_object_or_404(
        Restaurant,
        id=restaurant_id,
    )

    table = get_object_or_404(
        Table,
        id=table_id,
        restaurant=restaurant,
        is_active=True,
    )

    item = get_object_or_404(
        MenuItem,
        id=item_id,
        category__restaurant=restaurant,
        is_available=True,
    )

    portions = calculate_item_available_portions(item)
    if portions is not None and portions < 1:
        messages.error(request, f"Sorry, '{item.name}' is currently sold out.")
        if request.POST.get("return_to") == "detail":
            return redirect(
                "item_detail",
                restaurant_id=restaurant.id,
                table_id=table.id,
                item_id=item.id,
            )
        return redirect(
            "customer_menu",
            restaurant_id=restaurant.id,
            table_id=table.id,
        )

    return_to = request.POST.get(
        "return_to",
        "menu",
    )

    # If adding directly from menu list and item has required addon groups, redirect to detail to choose
    has_required_addons = item.addon_groups.filter(
        is_active=True,
        is_required=True,
        restaurant=restaurant,
    ).exists()
    if return_to != "detail" and has_required_addons:
        messages.info(request, f"Please select your required options for '{item.name}'.")
        return redirect(
            "item_detail",
            restaurant_id=restaurant.id,
            table_id=table.id,
            item_id=item.id,
        )

    # Collect selected addon IDs from POST (checkboxes 'addons' + radio fields 'addon_group_<id>')
    selected_addon_ids = list(request.POST.getlist("addons"))
    for key, val in request.POST.items():
        if key.startswith("addon_group_") and val:
            selected_addon_ids.append(val)

    try:
        validated_addons, unit_addons_total = validate_item_addons(item, selected_addon_ids)
    except ValidationError as err:
        messages.error(request, validation_error_message(err))
        return redirect(
            "item_detail",
            restaurant_id=restaurant.id,
            table_id=table.id,
            item_id=item.id,
        )

    sorted_addon_ids = sorted(opt.id for opt in validated_addons)
    if sorted_addon_ids:
        item_key = f"{item.id}:{','.join(str(i) for i in sorted_addon_ids)}"
    else:
        item_key = str(item.id)

    addon_summaries = [
        {
            "id": opt.id,
            "name": opt.name,
            "price": float(opt.price),
            "group": opt.group.name,
        }
        for opt in validated_addons
    ]

    cart = get_table_cart(
        request,
        restaurant_id,
        table_id,
    )

    base_price = float(item.price)
    unit_price = float(item.price + unit_addons_total)

    if item_key in cart:
        cart[item_key]["quantity"] += 1
        cart[item_key]["price"] = base_price
        cart[item_key]["unit_price"] = unit_price
        cart[item_key]["unit_addons_total"] = float(unit_addons_total)
        cart[item_key]["addons"] = addon_summaries
        cart[item_key]["addon_ids"] = sorted_addon_ids
    else:
        cart[item_key] = {
            "id": item.id,
            "name": item.name,
            "price": base_price,
            "unit_price": unit_price,
            "unit_addons_total": float(unit_addons_total),
            "quantity": 1,
            "addons": addon_summaries,
            "addon_ids": sorted_addon_ids,
        }

    save_table_cart(
        request,
        restaurant_id,
        table_id,
        cart,
    )

    if return_to == "detail":
        messages.success(request, f"Added '{item.name}' to your order.")
        return redirect(
            "item_detail",
            restaurant_id=restaurant.id,
            table_id=table.id,
            item_id=item.id,
        )

    return redirect(
        "customer_menu",
        restaurant_id=restaurant.id,
        table_id=table.id,
    )


# ============================================================
# CART
# ============================================================

def cart_view(
    request,
    restaurant_id,
    table_id,
):
    restaurant = get_object_or_404(
        Restaurant,
        id=restaurant_id,
    )

    table = get_object_or_404(
        Table,
        id=table_id,
        restaurant=restaurant,
        is_active=True,
    )

    cart = get_table_cart(
        request,
        restaurant_id,
        table_id,
    )

    total = calculate_cart_total(
        cart
    )

    return render(
        request,
        "customer/cart.html",
        {
            "restaurant": restaurant,
            "table": table,
            "cart": cart,
            "total": total,
        },
    )


def increase_cart_item(
    request,
    restaurant_id,
    table_id,
    item_id,
):
    cart = get_table_cart(
        request,
        restaurant_id,
        table_id,
    )

    item_key = str(
        item_id
    )

    if item_key in cart:

        cart[item_key]["quantity"] += 1

        save_table_cart(
            request,
            restaurant_id,
            table_id,
            cart,
        )

    return redirect(
        "cart",
        restaurant_id=restaurant_id,
        table_id=table_id,
    )


def decrease_cart_item(
    request,
    restaurant_id,
    table_id,
    item_id,
):
    cart = get_table_cart(
        request,
        restaurant_id,
        table_id,
    )

    item_key = str(
        item_id
    )

    if item_key in cart:

        cart[item_key]["quantity"] -= 1

        if (
            cart[item_key]["quantity"]
            <= 0
        ):
            del cart[
                item_key
            ]

        save_table_cart(
            request,
            restaurant_id,
            table_id,
            cart,
        )

    return redirect(
        "cart",
        restaurant_id=restaurant_id,
        table_id=table_id,
    )


def remove_cart_item(
    request,
    restaurant_id,
    table_id,
    item_id,
):
    cart = get_table_cart(
        request,
        restaurant_id,
        table_id,
    )

    item_key = str(
        item_id
    )

    if item_key in cart:

        del cart[
            item_key
        ]

        save_table_cart(
            request,
            restaurant_id,
            table_id,
            cart,
        )

    return redirect(
        "cart",
        restaurant_id=restaurant_id,
        table_id=table_id,
    )


# ============================================================
# CHECKOUT
# ============================================================

def _prepare_checkout_items(restaurant, cart):
    validated_items = []
    for item_id, item_data in cart.items():
        if not isinstance(item_data, dict):
            return None, "Invalid cart item. Please update your cart."
        quantity_text = str(item_data.get("quantity", ""))
        if not quantity_text.isascii() or not quantity_text.isdigit():
            return None, "Every cart quantity must be a positive whole number."
        quantity = int(quantity_text)
        if not 1 <= quantity <= 2147483647:
            return None, "Every cart quantity must be a positive whole number."

        raw_item_id = str(item_id).split(":")[0].strip()
        if not raw_item_id.isascii() or not raw_item_id.isdigit():
            return None, "Invalid menu item. Please update your cart."

        try:
            menu_item = MenuItem.objects.get(
                pk=int(raw_item_id),
                category__restaurant=restaurant,
                is_available=True,
            )
        except MenuItem.DoesNotExist:
            return None, "One or more items in your cart are no longer available."

        addon_ids = item_data.get("addon_ids", [])
        try:
            validated_addons, unit_addons_total = validate_item_addons(menu_item, addon_ids)
        except ValidationError as e:
            return None, validation_error_message(e)

        line_unit_price = menu_item.price + unit_addons_total
        validated_items.append(
            {
                "menu_item": menu_item,
                "quantity": quantity,
                "price": menu_item.price,
                "addons": validated_addons,
                "line_unit_price": line_unit_price,
                "subtotal": line_unit_price * quantity,
            }
        )

    if not validated_items:
        return None, "Your cart is empty."
    return validated_items, None


def checkout(
    request,
    restaurant_id,
    table_id,
):
    restaurant = get_object_or_404(
        Restaurant,
        id=restaurant_id,
    )

    table = get_object_or_404(
        Table,
        id=table_id,
        restaurant=restaurant,
        is_active=True,
    )

    cart = get_table_cart(
        request,
        restaurant_id,
        table_id,
    )

    if not cart:
        return redirect(
            "cart",
            restaurant_id=restaurant_id,
            table_id=table_id,
        )

    operating_settings = resolve_settings(restaurant, table.branch)

    validated_items, prep_error = _prepare_checkout_items(restaurant, cart)
    if prep_error:
        return render(
            request,
            "customer/checkout.html",
            {
                "restaurant": restaurant,
                "table": table,
                "cart": cart,
                "total": calculate_cart_total(cart),
                "payment_method_choices": payment_choices(operating_settings),
                "operating_settings": operating_settings,
                "stock_error": prep_error,
            },
            status=400,
        )

    coupon_code = (
        request.POST.get("coupon_code", "").strip()
        if request.method == "POST"
        else request.GET.get("coupon_code", "").strip()
    )
    coupon_error = None
    try:
        pricing = calculate_order_pricing(
            restaurant=restaurant,
            branch=table.branch,
            settings_values=operating_settings,
            items_data=validated_items,
            coupon_code=coupon_code or None,
        )
    except ValidationError as e:
        coupon_error = validation_error_message(e)
        pricing = calculate_order_pricing(
            restaurant=restaurant,
            branch=table.branch,
            settings_values=operating_settings,
            items_data=validated_items,
            coupon_code=None,
        )

    table_session_summary = _get_table_session_summary(table, pricing["total_amount"])

    if request.method == "POST":
        order_type = request.POST.get("order_type", "DINE_IN")
        payment_timing = request.POST.get("payment_timing", "PAY_LATER").strip().upper()
        if payment_timing not in {"PAY_NOW", "PAY_LATER"}:
            payment_timing = "PAY_LATER"
        payment_method = request.POST.get("payment_method", "").strip().upper()
        payment_reference = request.POST.get("payment_reference", "").strip()

        if order_type not in dict(Order.ORDER_TYPES):
            return render(
                request,
                "customer/checkout.html",
                {
                    "restaurant": restaurant,
                    "table": table,
                    "cart": cart,
                    "total": pricing["total_amount"],
                    "pricing": pricing,
                    "table_session_summary": table_session_summary,
                    "payment_method_choices": payment_choices(operating_settings),
                    "operating_settings": operating_settings,
                    "stock_error": "Please select a valid order type.",
                    "coupon_code": coupon_code,
                    "coupon_error": coupon_error,
                },
                status=400,
            )

        if coupon_code and coupon_error:
            return render(
                request,
                "customer/checkout.html",
                {
                    "restaurant": restaurant,
                    "table": table,
                    "cart": cart,
                    "total": pricing["total_amount"],
                    "pricing": pricing,
                    "table_session_summary": table_session_summary,
                    "payment_method_choices": payment_choices(operating_settings),
                    "operating_settings": operating_settings,
                    "stock_error": coupon_error,
                    "coupon_code": coupon_code,
                    "coupon_error": coupon_error,
                },
                status=400,
            )

        if payment_timing == "PAY_NOW":
            valid_methods = {m for m, _ in Order.PAYMENT_METHOD_CHOICES}
            if payment_method not in valid_methods:
                return render(
                    request,
                    "customer/checkout.html",
                    {
                        "restaurant": restaurant,
                        "table": table,
                        "cart": cart,
                        "total": pricing["total_amount"],
                        "pricing": pricing,
                        "table_session_summary": table_session_summary,
                        "payment_method_choices": payment_choices(operating_settings),
                        "operating_settings": operating_settings,
                        "stock_error": "Please select a payment method.",
                        "coupon_code": coupon_code,
                        "coupon_error": coupon_error,
                    },
                    status=400,
                )

        try:
            validate_new_order(operating_settings, order_type, payment_timing, payment_method)
        except ValidationError as error:
            return render(request, "customer/checkout.html", {
                "restaurant": restaurant, "table": table, "cart": cart, "total": pricing["total_amount"],
                "pricing": pricing, "operating_settings": operating_settings,
                "table_session_summary": table_session_summary,
                "payment_method_choices": payment_choices(operating_settings),
                "stock_error": validation_error_message(error), "coupon_code": coupon_code,
            }, status=400)

        order_table = (
            table
            if order_type == "DINE_IN"
            else None
        )

        try:
            with transaction.atomic():
                if pricing["applied_coupon"]:
                    apply_coupon_usage_atomic(pricing["applied_coupon"].id)

                # Resolve branch: QR orders come from a physical table, so inherit its branch
                order_branch = getattr(table, "branch", None)

                order = Order.objects.create(
                    restaurant=restaurant,
                    branch=order_branch,
                    table=order_table,
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
                    payment_status="PAID" if payment_timing == "PAY_NOW" else "UNPAID",
                    payment_method=payment_method if payment_timing == "PAY_NOW" else "",
                    payment_reference=payment_reference if payment_timing == "PAY_NOW" else "",
                )

                if order_type == "DINE_IN" and order_table:
                    order_table.mark_occupied()

                for item_data in pricing["items_data"]:
                    order_item = OrderItem.objects.create(
                        order=order,
                        menu_item=item_data["menu_item"],
                        quantity=item_data["quantity"],
                        price=item_data["price"],
                        discount_amount=item_data.get("discount_amount", Decimal("0.00")),
                    )
                    for addon in item_data["addons"]:
                        OrderItemAddon.objects.create(
                            order_item=order_item,
                            addon_option=addon,
                            addon_group_name=addon.group.name if addon.group else "",
                            addon_name=addon.name,
                            price=addon.price,
                        )

                reserve_stock_for_order(order)

                if payment_timing == "PAY_NOW":
                    from orders.services import record_order_payment
                    record_order_payment(
                        order,
                        amount=pricing["total_amount"],
                        payment_method=payment_method or "CASH",
                        reference=payment_reference or "",
                        recorded_by=None,
                    )

                transaction.on_commit(
                    lambda order_id=order.pk: register_new_order(order_id)
                )

        except ValidationError as error:
            stock_error = validation_error_message(error)
            return render(
                request,
                "customer/checkout.html",
                {
                    "restaurant": restaurant,
                    "table": table,
                    "cart": cart,
                    "total": pricing["total_amount"],
                    "pricing": pricing,
                    "table_session_summary": table_session_summary,
                    "payment_method_choices": payment_choices(operating_settings),
                    "operating_settings": operating_settings,
                    "stock_error": stock_error,
                    "coupon_code": coupon_code,
                    "coupon_error": coupon_error,
                },
                status=409,
            )

        remember_customer_order(request, order)
        remember_qr_order(request, order)
        save_table_cart(request, restaurant_id, table_id, {})

        return redirect("order_success", order_id=order.id)

    return render(
        request,
        "customer/checkout.html",
        {
            "restaurant": restaurant,
            "table": table,
            "cart": cart,
            "total": pricing["total_amount"],
            "pricing": pricing,
            "table_session_summary": table_session_summary,
            "payment_method_choices": payment_choices(operating_settings),
            "operating_settings": operating_settings,
            "stock_error": None,
            "coupon_code": coupon_code,
            "coupon_error": coupon_error,
        },
    )




# ============================================================
# ORDER SUCCESS
# ============================================================

@never_cache
def order_success(
    request,
    order_id,
):
    order = get_object_or_404(
        Order.objects.select_related(
            "restaurant",
            "table",
        ).prefetch_related("items__menu_item__category"),
        id=order_id,
    )

    return render(
        request,
        "customer/order_success.html",
        {
            "order": order,
            "tracking": {**get_order_status_data(order), "feedback_url": qr_feedback_url(request, order)},
        },
    )
