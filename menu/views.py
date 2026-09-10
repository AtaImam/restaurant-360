from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from inventory.services import reserve_stock_for_order
from orders.models import Order, OrderItem
from restaurant.models import Restaurant, Table

from .models import Category, MenuItem
from .tracking_views import get_order_status_data


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
            price = Decimal(str(item.get("price", 0)))
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

    cart = get_table_cart(
        request,
        restaurant_id,
        table_id,
    )

    item_key = str(
        item.id
    )

    if item_key in cart:

        cart[item_key]["quantity"] += 1

        cart[item_key]["price"] = float(
            item.price
        )

        cart[item_key]["name"] = (
            item.name
        )

    else:

        cart[item_key] = {
            "name": item.name,
            "price": float(
                item.price
            ),
            "quantity": 1,
        }

    save_table_cart(
        request,
        restaurant_id,
        table_id,
        cart,
    )

    return_to = request.POST.get(
        "return_to",
        "menu",
    )

    if return_to == "detail":

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

    total = calculate_cart_total(
        cart
    )

    if request.method == "POST":
        order_type = request.POST.get("order_type", "DINE_IN")
        validated_items = []
        server_total = Decimal("0.00")

        try:
            if order_type not in dict(Order.ORDER_TYPES):
                raise ValidationError("Please select a valid order type.")

            for item_id, item_data in cart.items():
                if not isinstance(item_data, dict):
                    raise ValidationError("Invalid cart item. Please update your cart.")
                quantity_text = str(item_data.get("quantity", ""))
                if not quantity_text.isascii() or not quantity_text.isdigit():
                    raise ValidationError("Every cart quantity must be a positive whole number.")
                quantity = int(quantity_text)
                if not 1 <= quantity <= 2147483647:
                    raise ValidationError("Every cart quantity must be a positive whole number.")
                if not str(item_id).isascii() or not str(item_id).isdigit():
                    raise ValidationError("Invalid menu item. Please update your cart.")
                menu_item = MenuItem.objects.filter(
                    id=item_id,
                    category__restaurant=restaurant,
                    is_available=True,
                ).first()
                if menu_item is None:
                    raise ValidationError(
                        "One or more menu items are unavailable. Please update your cart."
                    )
                server_total += menu_item.price * quantity
                if server_total > Decimal("99999999.99"):
                    raise ValidationError("This order exceeds the supported total amount.")
                item_data.update(
                    name=menu_item.name,
                    price=str(menu_item.price),
                    subtotal=str(menu_item.price * quantity),
                )
                validated_items.append({
                    "menu_item": menu_item,
                    "quantity": quantity,
                    "price": menu_item.price,
                })
        except ValidationError as error:
            return render(
                request,
                "customer/checkout.html",
                {
                    "restaurant": restaurant,
                    "table": table,
                    "cart": cart,
                    "total": total,
                    "stock_error": validation_error_message(error),
                },
                status=400,
            )

        order_table = (
            table
            if order_type == "DINE_IN"
            else None
        )

        # ====================================================
        # CREATE ORDER + ITEMS + RESERVATION
        # ALL OR NOTHING
        # ====================================================

        try:

            with transaction.atomic():

                order = Order.objects.create(
                    restaurant=restaurant,
                    table=order_table,
                    order_type=order_type,
                    status="NEW",

                    subtotal=
                        server_total,

                    discount_amount=
                        Decimal("0.00"),

                    service_charge=
                        Decimal("0.00"),

                    vat_amount=
                        Decimal("0.00"),

                    total_amount=
                        server_total,

                    payment_status=
                        "UNPAID",
                )

                OrderItem.objects.bulk_create(
                    [
                        OrderItem(
                            order=order,

                            menu_item=(
                                item_data[
                                    "menu_item"
                                ]
                            ),

                            quantity=(
                                item_data[
                                    "quantity"
                                ]
                            ),

                            price=(
                                item_data[
                                    "price"
                                ]
                            ),
                        )

                        for item_data
                        in validated_items
                    ]
                )

                # --------------------------------------------
                # INVENTORY RESERVATION
                # --------------------------------------------

                reserve_stock_for_order(
                    order
                )

        except ValidationError as error:

            stock_error = (
                validation_error_message(
                    error
                )
            )

            return render(
                request,
                "customer/checkout.html",
                {
                    "restaurant":
                        restaurant,

                    "table":
                        table,

                    "cart":
                        cart,

                    "total":
                        server_total,

                    "stock_error":
                        stock_error,
                },
                status=409,
            )

        # Cart clears ONLY after everything succeeds.

        save_table_cart(
            request,
            restaurant_id,
            table_id,
            {},
        )

        return redirect(
            "order_success",
            order_id=order.id,
        )

    return render(
        request,
        "customer/checkout.html",
        {
            "restaurant": restaurant,
            "table": table,
            "cart": cart,
            "total": total,
            "stock_error": None,
        },
    )


# ============================================================
# ORDER SUCCESS
# ============================================================

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
            "tracking": get_order_status_data(order),
        },
    )
