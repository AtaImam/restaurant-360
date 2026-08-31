from django.shortcuts import get_object_or_404, redirect, render

from restaurant.models import Restaurant
from .models import Ingredient, IngredientCategory


def ingredient_list(request):
    ingredients = Ingredient.objects.select_related(
        'restaurant',
        'category',
        'storage_location'
    ).prefetch_related(
        'allergens'
    ).order_by('name')

    ingredient_rows = []

    low_stock_count = 0
    out_of_stock_count = 0
    ok_stock_count = 0

    for ingredient in ingredients:
        status = ingredient.stock_status

        if status == 'LOW':
            status_label = 'Low Stock'
            status_class = 'status-low'
            low_stock_count += 1

        elif status == 'OUT':
            status_label = 'Out of Stock'
            status_class = 'status-out'
            out_of_stock_count += 1

        else:
            status_label = 'OK'
            status_class = 'status-ok'
            ok_stock_count += 1

        ingredient_rows.append({
            'ingredient': ingredient,
            'status_label': status_label,
            'status_class': status_class,
            'reserved_stock': ingredient.reserved_stock,
            'available_stock': ingredient.available_stock,
            'reorder_quantity': ingredient.reorder_quantity,
            'unit_cost': ingredient.current_unit_cost,
        })

    context = {
        'ingredient_rows': ingredient_rows,
        'total_ingredients': len(ingredient_rows),
        'low_stock_count': low_stock_count,
        'out_of_stock_count': out_of_stock_count,
        'ok_stock_count': ok_stock_count,
    }

    return render(
        request,
        'inventory/ingredient_list.html',
        context
    )
def inventory_category_list(request):
    categories = IngredientCategory.objects.select_related(
        'restaurant'
    ).order_by('name')

    category_rows = []

    for category in categories:
        category_rows.append({
            'category': category,
            'item_count': category.ingredients.count(),
        })

    context = {
        'category_rows': category_rows,
    }

    return render(
        request,
        'inventory/category_list.html',
        context
    )
def inventory_category_create(request):
    restaurants = Restaurant.objects.all().order_by('name')

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        restaurant_id = request.POST.get('restaurant')

        if name and restaurant_id:
            IngredientCategory.objects.create(
                name=name,
                restaurant_id=restaurant_id
            )

            return redirect('inventory_category_list')

    context = {
        'restaurants': restaurants,
        'page_title': 'Add Inventory Category',
        'button_text': 'Create Category',
    }

    return render(
        request,
        'inventory/category_form.html',
        context
    )
def inventory_category_edit(request, category_id):
    category = get_object_or_404(
        IngredientCategory,
        id=category_id
    )

    restaurants = Restaurant.objects.all().order_by('name')

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        restaurant_id = request.POST.get('restaurant')

        if name and restaurant_id:
            category.name = name
            category.restaurant_id = restaurant_id
            category.save()

            return redirect('inventory_category_list')

    restaurant_rows = []

    for restaurant in restaurants:
        restaurant_rows.append({
            'id': restaurant.id,
            'name': restaurant.name,
            'selected': restaurant.id == category.restaurant_id,
        })

    context = {
        'category': category,
        'restaurants': restaurant_rows,
        'page_title': 'Edit Ingredient Group',
        'button_text': 'Save Changes',
    }

    return render(
        request,
        'inventory/category_form.html',
        context
    )

from decimal import Decimal, InvalidOperation

from django.db import IntegrityError
from django.shortcuts import get_object_or_404, redirect, render

from restaurant.models import Restaurant

from .models import (
    Allergen,
    Ingredient,
    IngredientCategory,
    IngredientPriceHistory,
    StockTransaction,
    StorageLocation,
)


def to_decimal(value, default="0"):
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


# =========================================================
# INGREDIENT GROUPS
# =========================================================


def inventory_category_list(request):
    categories = IngredientCategory.objects.select_related(
        "restaurant"
    ).order_by("name")

    category_rows = []

    for category in categories:
        category_rows.append(
            {
                "category": category,
                "item_count": category.ingredients.count(),
            }
        )

    return render(
        request,
        "inventory/category_list.html",
        {
            "category_rows": category_rows,
        },
    )


def inventory_category_create(request):
    restaurants = [
        {
            "id": restaurant.id,
            "name": restaurant.name,
            "selected": False,
        }
        for restaurant in Restaurant.objects.all().order_by("name")
    ]

    error_message = None

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        restaurant_id = request.POST.get("restaurant")

        if not name or not restaurant_id:
            error_message = "Group name and restaurant are required."

        else:
            try:
                IngredientCategory.objects.create(
                    name=name,
                    restaurant_id=restaurant_id,
                )

                return redirect("inventory_category_list")

            except IntegrityError:
                error_message = (
                    "This ingredient group already exists for this restaurant."
                )

    return render(
        request,
        "inventory/category_form.html",
        {
            "restaurants": restaurants,
            "page_title": "Add Ingredient Group",
            "button_text": "Create Group",
            "error_message": error_message,
        },
    )


def inventory_category_edit(request, category_id):
    category = get_object_or_404(
        IngredientCategory,
        id=category_id,
    )

    restaurants = [
        {
            "id": restaurant.id,
            "name": restaurant.name,
            "selected": restaurant.id == category.restaurant_id,
        }
        for restaurant in Restaurant.objects.all().order_by("name")
    ]

    error_message = None

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        restaurant_id = request.POST.get("restaurant")

        if not name or not restaurant_id:
            error_message = "Group name and restaurant are required."

        else:
            try:
                category.name = name
                category.restaurant_id = restaurant_id
                category.save()

                return redirect("inventory_category_list")

            except IntegrityError:
                error_message = (
                    "This ingredient group already exists for this restaurant."
                )

    return render(
        request,
        "inventory/category_form.html",
        {
            "category": category,
            "restaurants": restaurants,
            "page_title": "Edit Ingredient Group",
            "button_text": "Save Changes",
            "error_message": error_message,
        },
    )


def inventory_category_delete(request, category_id):
    category = get_object_or_404(
        IngredientCategory,
        id=category_id,
    )

    if request.method == "POST":
        if not category.ingredients.exists():
            category.delete()

    return redirect("inventory_category_list")


# =========================================================
# INVENTORY ITEMS
# =========================================================


def ingredient_list(request):
    ingredients = (
        Ingredient.objects.select_related(
            "restaurant",
            "category",
            "storage_location",
        )
        .prefetch_related("allergens")
        .order_by("name")
    )

    ingredient_rows = []

    low_stock_count = 0
    out_of_stock_count = 0
    ok_stock_count = 0
    inactive_count = 0

    for ingredient in ingredients:
        if not ingredient.is_active:
            status_label = "Inactive"
            status_class = "status-inactive"
            inactive_count += 1

        elif ingredient.stock_status == "LOW":
            status_label = "Low Stock"
            status_class = "status-low"
            low_stock_count += 1

        elif ingredient.stock_status == "OUT":
            status_label = "Out of Stock"
            status_class = "status-out"
            out_of_stock_count += 1

        else:
            status_label = "OK"
            status_class = "status-ok"
            ok_stock_count += 1

        ingredient_rows.append(
            {
                "ingredient": ingredient,
                "status_label": status_label,
                "status_class": status_class,
                "reserved_stock": ingredient.reserved_stock,
                "available_stock": ingredient.available_stock,
                "reorder_quantity": ingredient.reorder_quantity,
                "unit_cost": ingredient.current_unit_cost,
            }
        )

    return render(
        request,
        "inventory/ingredient_list.html",
        {
            "ingredient_rows": ingredient_rows,
            "total_ingredients": len(ingredient_rows),
            "low_stock_count": low_stock_count,
            "out_of_stock_count": out_of_stock_count,
            "ok_stock_count": ok_stock_count,
            "inactive_count": inactive_count,
        },
    )


def ingredient_create(request):
    restaurants = Restaurant.objects.all().order_by("name")
    categories = IngredientCategory.objects.select_related(
        "restaurant"
    ).order_by("name")

    restaurant_rows = [
        {
            "id": restaurant.id,
            "name": restaurant.name,
        }
        for restaurant in restaurants
    ]

    category_rows = [
        {
            "id": category.id,
            "name": category.name,
            "restaurant_name": category.restaurant.name,
        }
        for category in categories
    ]

    error_message = None

    if request.method == "POST":
        restaurant_id = request.POST.get("restaurant")
        category_id = request.POST.get("category")

        name = request.POST.get("name", "").strip()
        sku = request.POST.get("sku", "").strip().upper()
        description = request.POST.get("description", "").strip()

        storage_location_name = request.POST.get(
            "storage_location",
            "",
        ).strip()

        base_unit = request.POST.get("base_unit")

        pack_size = to_decimal(
            request.POST.get("pack_size"),
        )

        pack_price = to_decimal(
            request.POST.get("current_pack_price"),
        )

        current_stock = to_decimal(
            request.POST.get("current_stock"),
        )

        minimum_level = to_decimal(
            request.POST.get("minimum_level"),
        )

        target_level = to_decimal(
            request.POST.get("target_level"),
        )

        allergen_text = request.POST.get(
            "allergens",
            "",
        ).strip()

        is_active = request.POST.get("is_active") == "on"

        if not restaurant_id:
            error_message = "Restaurant is required."

        elif not category_id:
            error_message = "Ingredient group is required."

        elif not name:
            error_message = "Product name is required."

        elif not sku:
            error_message = "SKU is required."

        elif base_unit not in [
            Ingredient.BaseUnit.GRAM,
            Ingredient.BaseUnit.MILLILITRE,
            Ingredient.BaseUnit.PIECE,
        ]:
            error_message = "Please select a valid base unit."

        elif pack_size <= 0:
            error_message = "Pack size must be greater than zero."

        elif pack_price < 0:
            error_message = "Pack price cannot be negative."

        elif current_stock < 0:
            error_message = "Current stock cannot be negative."

        elif minimum_level < 0:
            error_message = "Minimum level cannot be negative."

        elif target_level < 0:
            error_message = "Target level cannot be negative."

        elif target_level < minimum_level:
            error_message = (
                "Target / par level should not be lower than minimum level."
            )

        else:
            category = get_object_or_404(
                IngredientCategory,
                id=category_id,
            )

            if str(category.restaurant_id) != str(restaurant_id):
                error_message = (
                    "Selected ingredient group does not belong "
                    "to the selected restaurant."
                )

            else:
                try:
                    storage_location = None

                    if storage_location_name:
                        storage_location, _ = (
                            StorageLocation.objects.get_or_create(
                                restaurant_id=restaurant_id,
                                name=storage_location_name,
                            )
                        )

                    ingredient = Ingredient.objects.create(
                        restaurant_id=restaurant_id,
                        category=category,
                        storage_location=storage_location,
                        name=name,
                        sku=sku,
                        description=description,
                        base_unit=base_unit,
                        pack_size=pack_size,
                        current_pack_price=pack_price,
                        current_stock=current_stock,
                        minimum_level=minimum_level,
                        target_level=target_level,
                        is_active=is_active,
                    )

                    if allergen_text:
                        allergen_names = [
                            allergen.strip()
                            for allergen in allergen_text.split(",")
                            if allergen.strip()
                        ]

                        for allergen_name in allergen_names:
                            allergen, _ = Allergen.objects.get_or_create(
                                name=allergen_name
                            )

                            ingredient.allergens.add(allergen)

                    IngredientPriceHistory.objects.create(
                        ingredient=ingredient,
                        pack_price=pack_price,
                        effective_date=request.POST.get(
                            "effective_date"
                        ),
                    )

                    if current_stock > 0:
                        StockTransaction.objects.create(
                            ingredient=ingredient,
                            transaction_type=(
                                StockTransaction.TransactionType.ADJUSTMENT_IN
                            ),
                            quantity=current_stock,
                            note="Opening stock",
                        )

                    return redirect(
                        "ingredient_detail",
                        ingredient_id=ingredient.id,
                    )

                except IntegrityError:
                    error_message = (
                        "An inventory item with this SKU already "
                        "exists for the selected restaurant."
                    )

    return render(
        request,
        "inventory/ingredient_form.html",
        {
            "page_title": "Add Inventory Item",
            "button_text": "Create Inventory Item",
            "restaurant_rows": restaurant_rows,
            "category_rows": category_rows,
            "error_message": error_message,
        },
    )


def ingredient_detail(request, ingredient_id):
    ingredient = get_object_or_404(
        Ingredient.objects.select_related(
            "restaurant",
            "category",
            "storage_location",
        ).prefetch_related(
            "allergens",
            "price_history",
            "stock_transactions",
        ),
        id=ingredient_id,
    )

    recent_transactions = ingredient.stock_transactions.all()[:10]
    price_history = ingredient.price_history.all()[:10]

    if not ingredient.is_active:
        status_label = "Inactive"
        status_class = "status-inactive"

    elif ingredient.stock_status == "LOW":
        status_label = "Low Stock"
        status_class = "status-low"

    elif ingredient.stock_status == "OUT":
        status_label = "Out of Stock"
        status_class = "status-out"

    else:
        status_label = "OK"
        status_class = "status-ok"

    return render(
        request,
        "inventory/ingredient_detail.html",
        {
            "ingredient": ingredient,
            "status_label": status_label,
            "status_class": status_class,
            "reserved_stock": ingredient.reserved_stock,
            "available_stock": ingredient.available_stock,
            "reorder_quantity": ingredient.reorder_quantity,
            "unit_cost": ingredient.current_unit_cost,
            "recent_transactions": recent_transactions,
            "price_history": price_history,
        },
    )


def ingredient_edit(request, ingredient_id):
    ingredient = get_object_or_404(
        Ingredient,
        id=ingredient_id,
    )

    restaurants = Restaurant.objects.all().order_by("name")
    categories = IngredientCategory.objects.select_related(
        "restaurant"
    ).order_by("name")

    restaurant_rows = [
        {
            "id": restaurant.id,
            "name": restaurant.name,
            "selected": restaurant.id == ingredient.restaurant_id,
        }
        for restaurant in restaurants
    ]

    category_rows = [
        {
            "id": category.id,
            "name": category.name,
            "restaurant_name": category.restaurant.name,
            "selected": category.id == ingredient.category_id,
        }
        for category in categories
    ]

    error_message = None

    if request.method == "POST":
        restaurant_id = request.POST.get("restaurant")
        category_id = request.POST.get("category")

        name = request.POST.get("name", "").strip()
        sku = request.POST.get("sku", "").strip().upper()
        description = request.POST.get("description", "").strip()

        storage_location_name = request.POST.get(
            "storage_location",
            "",
        ).strip()

        base_unit = request.POST.get("base_unit")

        pack_size = to_decimal(
            request.POST.get("pack_size"),
        )

        new_pack_price = to_decimal(
            request.POST.get("current_pack_price"),
        )

        new_stock = to_decimal(
            request.POST.get("current_stock"),
        )

        minimum_level = to_decimal(
            request.POST.get("minimum_level"),
        )

        target_level = to_decimal(
            request.POST.get("target_level"),
        )

        allergen_text = request.POST.get(
            "allergens",
            "",
        ).strip()

        is_active = request.POST.get("is_active") == "on"

        if not restaurant_id or not category_id:
            error_message = (
                "Restaurant and ingredient group are required."
            )

        elif not name or not sku:
            error_message = "Product name and SKU are required."

        elif pack_size <= 0:
            error_message = "Pack size must be greater than zero."

        elif new_pack_price < 0 or new_stock < 0:
            error_message = (
                "Price and stock cannot be negative."
            )

        elif minimum_level < 0 or target_level < 0:
            error_message = (
                "Stock levels cannot be negative."
            )

        elif target_level < minimum_level:
            error_message = (
                "Target / par level should not be lower than minimum level."
            )

        else:
            category = get_object_or_404(
                IngredientCategory,
                id=category_id,
            )

            if str(category.restaurant_id) != str(restaurant_id):
                error_message = (
                    "Selected ingredient group does not belong "
                    "to the selected restaurant."
                )

            else:
                old_stock = ingredient.current_stock
                old_pack_price = ingredient.current_pack_price

                try:
                    storage_location = None

                    if storage_location_name:
                        storage_location, _ = (
                            StorageLocation.objects.get_or_create(
                                restaurant_id=restaurant_id,
                                name=storage_location_name,
                            )
                        )

                    ingredient.restaurant_id = restaurant_id
                    ingredient.category = category
                    ingredient.storage_location = storage_location

                    ingredient.name = name
                    ingredient.sku = sku
                    ingredient.description = description

                    ingredient.base_unit = base_unit
                    ingredient.pack_size = pack_size
                    ingredient.current_pack_price = new_pack_price

                    ingredient.current_stock = new_stock
                    ingredient.minimum_level = minimum_level
                    ingredient.target_level = target_level

                    ingredient.is_active = is_active

                    ingredient.save()

                    ingredient.allergens.clear()

                    if allergen_text:
                        allergen_names = [
                            allergen.strip()
                            for allergen in allergen_text.split(",")
                            if allergen.strip()
                        ]

                        for allergen_name in allergen_names:
                            allergen, _ = Allergen.objects.get_or_create(
                                name=allergen_name
                            )

                            ingredient.allergens.add(allergen)

                    if new_pack_price != old_pack_price:
                        IngredientPriceHistory.objects.create(
                            ingredient=ingredient,
                            pack_price=new_pack_price,
                            effective_date=request.POST.get(
                                "effective_date"
                            ),
                        )

                    stock_difference = new_stock - old_stock

                    if stock_difference > 0:
                        StockTransaction.objects.create(
                            ingredient=ingredient,
                            transaction_type=(
                                StockTransaction.TransactionType.ADJUSTMENT_IN
                            ),
                            quantity=stock_difference,
                            note="Manual stock adjustment from item edit",
                        )

                    elif stock_difference < 0:
                        StockTransaction.objects.create(
                            ingredient=ingredient,
                            transaction_type=(
                                StockTransaction.TransactionType.ADJUSTMENT_OUT
                            ),
                            quantity=abs(stock_difference),
                            note="Manual stock adjustment from item edit",
                        )

                    return redirect(
                        "ingredient_detail",
                        ingredient_id=ingredient.id,
                    )

                except IntegrityError:
                    error_message = (
                        "An inventory item with this SKU already "
                        "exists for the selected restaurant."
                    )

    allergen_value = ", ".join(
        ingredient.allergens.values_list(
            "name",
            flat=True,
        )
    )

    return render(
        request,
        "inventory/ingredient_form.html",
        {
            "ingredient": ingredient,
            "page_title": "Edit Inventory Item",
            "button_text": "Save Changes",
            "restaurant_rows": restaurant_rows,
            "category_rows": category_rows,
            "allergen_value": allergen_value,
            "error_message": error_message,
        },
    )


def ingredient_toggle_active(request, ingredient_id):
    ingredient = get_object_or_404(
        Ingredient,
        id=ingredient_id,
    )

    if request.method == "POST":
        ingredient.is_active = not ingredient.is_active
        ingredient.save(
            update_fields=[
                "is_active",
                "updated_at",
            ]
        )

    return redirect(
        "ingredient_detail",
        ingredient_id=ingredient.id,
    )
def inventory_category_delete(request, category_id):
    category = get_object_or_404(
        IngredientCategory,
        id=category_id
    )

    if request.method == 'POST':
        if not category.ingredients.exists():
            category.delete()

    return redirect('inventory_category_list')