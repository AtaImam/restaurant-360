from decimal import Decimal, InvalidOperation

from functools import wraps
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone


def inventory_staff_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('/auth/login/')
        if request.user.is_superuser:
            return view_func(request, *args, **kwargs)
        if request.user.role == "waiter":
            raise PermissionDenied("Waiters do not have permission to access inventory.")
        return view_func(request, *args, **kwargs)
    return wrapper

from restaurant.models import Restaurant

from .models import (
    Allergen,
    Ingredient,
    IngredientCategory,
    IngredientPriceHistory,
    IngredientRequest,
    PurchaseOrder,
    PurchaseOrderItem,
    StockCount,
    StockTransaction,
    StorageLocation,
    Supplier,
    WasteRecord,
)
from .services import (
    record_stock_count_audit,
    record_stock_purchase,
    record_stock_waste,
)


def to_decimal(value, default="0"):
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


# =========================================================
# INGREDIENT GROUPS
# =========================================================


@login_required(login_url='/auth/login/')
@inventory_staff_required
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


@login_required(login_url='/auth/login/')
@inventory_staff_required
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


@login_required(login_url='/auth/login/')
@inventory_staff_required
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


@login_required(login_url='/auth/login/')
@inventory_staff_required
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


@login_required(login_url='/auth/login/')
@inventory_staff_required
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


@login_required(login_url='/auth/login/')
@inventory_staff_required
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

                    effective_date = request.POST.get("effective_date") or timezone.localdate()

                    IngredientPriceHistory.objects.create(
                        ingredient=ingredient,
                        pack_price=pack_price,
                        effective_date=effective_date,
                    )

                    if current_stock > 0:
                        StockTransaction.objects.create(
                            ingredient=ingredient,
                            restaurant=ingredient.restaurant,
                            transaction_type=(
                                StockTransaction.TransactionType.OPENING_BALANCE
                            ),
                            quantity=current_stock,
                            unit_cost_snapshot=ingredient.current_unit_cost,
                            note="Initial opening balance",
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


@login_required(login_url='/auth/login/')
@inventory_staff_required
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


@login_required(login_url='/auth/login/')
@inventory_staff_required
def ingredient_edit(request, ingredient_id):
    ingredient = get_object_or_404(Ingredient, id=ingredient_id)
    error_message = None

    if request.method == "POST":
        try:
            with transaction.atomic():
                ingredient = Ingredient.objects.select_for_update().get(id=ingredient_id)
                old_stock = ingredient.current_stock
                old_pack_price = ingredient.current_pack_price
                restaurant_id = request.POST.get("restaurant")
                category_id = request.POST.get("category")
                if not restaurant_id or not category_id:
                    raise ValidationError("Restaurant and ingredient group are required.")
                try:
                    category = IngredientCategory.objects.get(
                        id=category_id, restaurant_id=restaurant_id,
                    )
                except (IngredientCategory.DoesNotExist, TypeError, ValueError):
                    raise ValidationError("Choose an ingredient group belonging to the selected restaurant.")

                base_unit = request.POST.get("base_unit")
                identity_changed = (
                    str(ingredient.restaurant_id) != str(restaurant_id)
                    or ingredient.base_unit != base_unit
                )
                if identity_changed and (
                    ingredient.recipe_usages.exists()
                    or ingredient.stock_reservations.exists()
                    or ingredient.stock_transactions.exists()
                ):
                    raise ValidationError(
                        "Restaurant and base unit cannot change after this ingredient is used "
                        "in a recipe, reservation or stock transaction."
                    )

                numeric_fields = (
                    "pack_size", "current_pack_price", "current_stock", "minimum_level", "target_level",
                )
                try:
                    values = {
                        field: Decimal(request.POST.get(field, "0"))
                        for field in numeric_fields
                    }
                    if not all(value.is_finite() for value in values.values()):
                        raise ValueError
                    original_text = request.POST.get("original_current_stock")
                    original_stock = Decimal(original_text) if original_text is not None else None
                    if original_stock is not None and not original_stock.is_finite():
                        raise ValueError
                except (InvalidOperation, ValueError, TypeError):
                    raise ValidationError("Enter valid finite numbers for stock, pack size and prices.")

                new_stock = values["current_stock"]
                if original_stock is not None:
                    if new_stock == original_stock:
                        # Metadata edits must not undo consumption since the form loaded.
                        new_stock = old_stock
                    elif old_stock != original_stock:
                        raise ValidationError(
                            "Stock changed since this form was loaded. Reload before adjusting stock."
                        )
                if new_stock != old_stock and new_stock < ingredient.reserved_stock:
                    raise ValidationError("Physical stock cannot be lower than active order reservations.")
                if values["target_level"] < values["minimum_level"]:
                    raise ValidationError("Target / par level should not be lower than minimum level.")

                storage_location = None
                storage_name = request.POST.get("storage_location", "").strip()
                if storage_name:
                    storage_location, _ = StorageLocation.objects.get_or_create(
                        restaurant_id=restaurant_id, name=storage_name,
                    )
                ingredient.restaurant_id = restaurant_id
                ingredient.category = category
                ingredient.storage_location = storage_location
                ingredient.name = request.POST.get("name", "").strip()
                ingredient.sku = request.POST.get("sku", "").strip().upper()
                ingredient.description = request.POST.get("description", "").strip()
                ingredient.base_unit = base_unit
                ingredient.is_active = request.POST.get("is_active") == "on"
                for field, value in values.items():
                    setattr(ingredient, field, value)
                ingredient.current_stock = new_stock
                ingredient.full_clean()
                ingredient.save()

                allergens = []
                for allergen_name in request.POST.get("allergens", "").split(","):
                    if allergen_name.strip():
                        allergen, _ = Allergen.objects.get_or_create(name=allergen_name.strip())
                        allergens.append(allergen)
                ingredient.allergens.set(allergens)

                if ingredient.current_pack_price != old_pack_price:
                    price_history = IngredientPriceHistory(
                        ingredient=ingredient,
                        pack_price=ingredient.current_pack_price,
                        effective_date=request.POST.get("effective_date"),
                    )
                    price_history.full_clean()
                    price_history.save()
                stock_difference = new_stock - old_stock
                if stock_difference:
                    StockTransaction.objects.create(
                        ingredient=ingredient,
                        transaction_type=(
                            StockTransaction.TransactionType.ADJUSTMENT_IN
                            if stock_difference > 0
                            else StockTransaction.TransactionType.ADJUSTMENT_OUT
                        ),
                        quantity=abs(stock_difference),
                        note="Manual stock adjustment from item edit",
                    )
            return redirect("ingredient_detail", ingredient_id=ingredient.id)
        except ValidationError as error:
            error_message = " ".join(error.messages)
        except IntegrityError:
            error_message = "An inventory item with this SKU already exists for the selected restaurant."
        ingredient.refresh_from_db()

    restaurant_rows = [
        {"id": restaurant.id, "name": restaurant.name,
         "selected": restaurant.id == ingredient.restaurant_id}
        for restaurant in Restaurant.objects.order_by("name")
    ]
    category_rows = [
        {"id": category.id, "name": category.name,
         "restaurant_name": category.restaurant.name,
         "selected": category.id == ingredient.category_id}
        for category in IngredientCategory.objects.select_related("restaurant").order_by("name")
    ]
    return render(request, "inventory/ingredient_form.html", {
        "ingredient": ingredient,
        "page_title": "Edit Inventory Item",
        "button_text": "Save Changes",
        "restaurant_rows": restaurant_rows,
        "category_rows": category_rows,
        "allergen_value": ", ".join(ingredient.allergens.values_list("name", flat=True)),
        "error_message": error_message,
    }, status=400 if error_message else 200)


@login_required(login_url='/auth/login/')
@inventory_staff_required
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


# =========================================================
# SUPPLIERS
# =========================================================

@login_required(login_url='/auth/login/')
@inventory_staff_required
def supplier_list(request):
    suppliers = Supplier.objects.select_related("restaurant").order_by("name")
    if getattr(request.user, "restaurant_id", None):
        suppliers = suppliers.filter(restaurant_id=request.user.restaurant_id)
    return render(request, "inventory/supplier_list.html", {"suppliers": suppliers})


def supplier_create(request):
    restaurants = Restaurant.objects.all().order_by("name")
    error_message = None
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        contact_name = request.POST.get("contact_name", "").strip()
        phone = request.POST.get("phone", "").strip()
        email = request.POST.get("email", "").strip()
        address = request.POST.get("address", "").strip()
        restaurant_id = request.POST.get("restaurant") or getattr(request.user, "restaurant_id", None)
        if not name:
            error_message = "Supplier name is required."
        elif not restaurant_id:
            error_message = "Please select a restaurant."
        else:
            try:
                Supplier.objects.create(
                    restaurant_id=restaurant_id,
                    name=name,
                    contact_name=contact_name,
                    phone=phone,
                    email=email,
                    address=address,
                )
                messages.success(request, f"Supplier '{name}' created successfully.")
                return redirect("supplier_list")
            except IntegrityError:
                error_message = f"Supplier '{name}' already exists for this restaurant."
    return render(request, "inventory/supplier_form.html", {
        "restaurants": restaurants,
        "page_title": "Add Supplier",
        "button_text": "Create Supplier",
        "error_message": error_message,
        "form_data": request.POST if request.method == "POST" else {},
    })


@login_required(login_url='/auth/login/')
@inventory_staff_required
def supplier_edit(request, supplier_id):
    suppliers = Supplier.objects.select_related("restaurant")
    if getattr(request.user, "restaurant_id", None):
        suppliers = suppliers.filter(restaurant_id=request.user.restaurant_id)
    supplier = get_object_or_404(suppliers, id=supplier_id)
    restaurants = Restaurant.objects.all().order_by("name")
    error_message = None
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        contact_name = request.POST.get("contact_name", "").strip()
        phone = request.POST.get("phone", "").strip()
        email = request.POST.get("email", "").strip()
        address = request.POST.get("address", "").strip()
        restaurant_id = request.POST.get("restaurant") or supplier.restaurant_id
        if not name:
            error_message = "Supplier name is required."
        else:
            try:
                supplier.restaurant_id = restaurant_id
                supplier.name = name
                supplier.contact_name = contact_name
                supplier.phone = phone
                supplier.email = email
                supplier.address = address
                supplier.save()
                messages.success(request, f"Supplier '{name}' updated successfully.")
                return redirect("supplier_list")
            except IntegrityError:
                error_message = f"Supplier '{name}' already exists for this restaurant."
    return render(request, "inventory/supplier_form.html", {
        "supplier": supplier,
        "restaurants": restaurants,
        "page_title": "Edit Supplier",
        "button_text": "Update Supplier",
        "error_message": error_message,
        "form_data": {
            "name": supplier.name,
            "contact_name": supplier.contact_name,
            "phone": supplier.phone,
            "email": supplier.email,
            "address": supplier.address,
            "restaurant": str(supplier.restaurant_id),
        } if request.method != "POST" else request.POST,
    })


@login_required(login_url='/auth/login/')
@inventory_staff_required
def supplier_toggle(request, supplier_id):
    suppliers = Supplier.objects.all()
    if getattr(request.user, "restaurant_id", None):
        suppliers = suppliers.filter(restaurant_id=request.user.restaurant_id)
    supplier = get_object_or_404(suppliers, id=supplier_id)
    if request.method == "POST":
        supplier.is_active = not supplier.is_active
        supplier.save(update_fields=["is_active"])
        messages.success(request, f"Supplier '{supplier.name}' marked as {'Active' if supplier.is_active else 'Inactive'}.")
    return redirect("supplier_list")


# =========================================================
# PURCHASES
# =========================================================

@login_required(login_url='/auth/login/')
@inventory_staff_required
def purchase_list(request):
    if not request.user.is_superuser and request.user.role in ["chief", "kitchen_manager", "waiter"]:
        raise PermissionDenied("Kitchen staff do not have permission to manage purchase orders.")
    purchases = PurchaseOrder.objects.select_related("supplier", "restaurant", "received_by").order_by("-purchase_date", "-id")
    if getattr(request.user, "restaurant_id", None):
        purchases = purchases.filter(restaurant_id=request.user.restaurant_id)
    return render(request, "inventory/purchase_list.html", {"purchases": purchases})


def purchase_create(request):
    if not request.user.is_superuser and (not request.user.is_authenticated or request.user.role in ["chief", "kitchen_manager", "waiter"]):
        raise PermissionDenied("Kitchen staff do not have permission to create purchase orders.")
    restaurant = getattr(request.user, "restaurant", None) or Restaurant.objects.first()
    suppliers = Supplier.objects.filter(is_active=True).order_by("name")
    ingredients = Ingredient.objects.filter(is_active=True).order_by("name")
    if restaurant:
        suppliers = suppliers.filter(restaurant=restaurant)
        ingredients = ingredients.filter(restaurant=restaurant)

    error_message = None
    if request.method == "POST":
        supplier_id = request.POST.get("supplier")
        invoice_number = request.POST.get("invoice_number", "").strip()
        purchase_date = request.POST.get("purchase_date") or timezone.now().date()
        note = request.POST.get("note", "").strip()
        action = request.POST.get("action", "save_draft")

        ingredient_ids = request.POST.getlist("ingredient_id")
        pack_quantities = request.POST.getlist("pack_quantity")
        pack_prices = request.POST.getlist("pack_price")

        if not supplier_id:
            error_message = "Please select a supplier."
        elif not ingredient_ids or len(ingredient_ids) != len(pack_quantities):
            error_message = "Please add at least one valid purchase item."
        else:
            try:
                supplier = suppliers.get(id=supplier_id)
            except Supplier.DoesNotExist:
                supplier = None
                error_message = "Selected supplier does not exist."

            if supplier:
                items_to_create = []
                total_amount = Decimal("0.00")
                try:
                    for ing_id, qty_str, price_str in zip(ingredient_ids, pack_quantities, pack_prices):
                        ing_id = ing_id.strip()
                        qty_str = qty_str.strip()
                        price_str = price_str.strip()
                        if not ing_id and not qty_str and not price_str:
                            continue
                        if not ing_id:
                            raise ValidationError("Ingredient is required for all item rows.")
                        ing = ingredients.get(id=ing_id)
                        qty = Decimal(qty_str)
                        price = Decimal(price_str)
                        if qty <= 0 or not qty.is_finite():
                            raise ValidationError("Pack quantity must be positive.")
                        if price < 0 or not price.is_finite():
                            raise ValidationError("Pack price cannot be negative.")
                        line_total = qty * price
                        total_amount += line_total
                        items_to_create.append({
                            "ingredient": ing,
                            "pack_quantity": qty,
                            "pack_price": price,
                            "total_price": line_total,
                        })
                    if not items_to_create:
                        raise ValidationError("Please add at least one purchase item.")

                    with transaction.atomic():
                        po = PurchaseOrder.objects.create(
                            restaurant=supplier.restaurant,
                            supplier=supplier,
                            invoice_number=invoice_number,
                            purchase_date=purchase_date,
                            status=PurchaseOrder.Status.DRAFT,
                            total_amount=total_amount,
                            note=note,
                        )
                        for item_data in items_to_create:
                            PurchaseOrderItem.objects.create(
                                purchase_order=po,
                                **item_data
                            )
                        if action == "receive_now":
                            record_stock_purchase(po, user=request.user if request.user.is_authenticated else None)
                            messages.success(request, f"Purchase order #{po.id} created and stock received successfully.")
                        else:
                            messages.success(request, f"Purchase order #{po.id} saved as draft.")
                        return redirect("purchase_detail", purchase_id=po.id)
                except ValidationError as e:
                    error_message = " ".join(e.messages) if hasattr(e, "messages") else str(e)
                except Exception as e:
                    error_message = str(e)

    return render(request, "inventory/purchase_form.html", {
        "suppliers": suppliers,
        "ingredients": ingredients,
        "error_message": error_message,
        "today": timezone.now().date().isoformat(),
    })


@login_required(login_url='/auth/login/')
@inventory_staff_required
def purchase_detail(request, purchase_id):
    if not request.user.is_superuser and request.user.role in ["chief", "kitchen_manager", "waiter"]:
        raise PermissionDenied("Kitchen staff do not have permission to view purchase orders.")
    purchases = PurchaseOrder.objects.select_related("supplier", "restaurant", "received_by").prefetch_related("items__ingredient")
    if getattr(request.user, "restaurant_id", None):
        purchases = purchases.filter(restaurant_id=request.user.restaurant_id)
    purchase = get_object_or_404(purchases, id=purchase_id)
    return render(request, "inventory/purchase_detail.html", {"purchase": purchase})


@login_required(login_url='/auth/login/')
@inventory_staff_required
def purchase_receive(request, purchase_id):
    if not request.user.is_superuser and request.user.role in ["chief", "kitchen_manager", "waiter"]:
        raise PermissionDenied("Kitchen staff do not have permission to receive purchase orders.")
    purchases = PurchaseOrder.objects.select_related("supplier", "restaurant")
    if getattr(request.user, "restaurant_id", None):
        purchases = purchases.filter(restaurant_id=request.user.restaurant_id)
    purchase = get_object_or_404(purchases, id=purchase_id)
    if request.method == "POST":
        try:
            record_stock_purchase(purchase, user=request.user if request.user.is_authenticated else None)
            messages.success(request, f"Purchase order #{purchase.id} received and stock updated successfully.")
        except ValidationError as e:
            messages.error(request, " ".join(e.messages) if hasattr(e, "messages") else str(e))
    return redirect("purchase_detail", purchase_id=purchase.id)


# =========================================================
# WASTAGE
# =========================================================

@login_required(login_url='/auth/login/')
@inventory_staff_required
def waste_list(request):
    records = WasteRecord.objects.select_related("ingredient", "ingredient__restaurant", "order").order_by("-created_at", "-id")
    if getattr(request.user, "restaurant_id", None):
        records = records.filter(ingredient__restaurant_id=request.user.restaurant_id)
    return render(request, "inventory/waste_list.html", {"records": records})


def waste_create(request):
    restaurant = getattr(request.user, "restaurant", None) or Restaurant.objects.first()
    ingredients = Ingredient.objects.filter(is_active=True).order_by("name")
    if restaurant:
        ingredients = ingredients.filter(restaurant=restaurant)

    selected_ingredient_id = request.GET.get("ingredient", "")
    error_message = None
    if request.method == "POST":
        ing_id = request.POST.get("ingredient")
        qty_str = request.POST.get("quantity", "").strip()
        reason = request.POST.get("reason", WasteRecord.Reason.SPOILAGE)
        note = request.POST.get("note", "").strip()
        if not ing_id:
            error_message = "Please select an ingredient."
        else:
            try:
                ing = ingredients.get(id=ing_id)
                qty = Decimal(qty_str)
                record_stock_waste(
                    ingredient=ing,
                    quantity=qty,
                    reason=reason,
                    note=note,
                    user=request.user if request.user.is_authenticated else None,
                )
                messages.success(request, f"Wasted {qty} {ing.cost_unit} of '{ing.name}' recorded.")
                return redirect("waste_list")
            except (ValidationError, InvalidOperation, ValueError) as e:
                error_message = " ".join(e.messages) if hasattr(e, "messages") else str(e)
            except Ingredient.DoesNotExist:
                error_message = "Selected ingredient not found."
    return render(request, "inventory/waste_form.html", {
        "ingredients": ingredients,
        "selected_ingredient_id": selected_ingredient_id,
        "reasons": WasteRecord.Reason.choices,
        "error_message": error_message,
    })


# =========================================================
# STOCK COUNTS / AUDIT
# =========================================================

@login_required(login_url='/auth/login/')
@inventory_staff_required
def stock_count_list(request):
    counts = StockCount.objects.select_related("ingredient", "ingredient__restaurant").order_by("-counted_at", "-id")
    if getattr(request.user, "restaurant_id", None):
        counts = counts.filter(ingredient__restaurant_id=request.user.restaurant_id)
    return render(request, "inventory/stock_count_list.html", {"counts": counts})


def stock_count_create(request):
    restaurant = getattr(request.user, "restaurant", None) or Restaurant.objects.first()
    ingredients = Ingredient.objects.filter(is_active=True).order_by("name")
    if restaurant:
        ingredients = ingredients.filter(restaurant=restaurant)

    selected_ingredient_id = request.GET.get("ingredient", "")
    error_message = None
    if request.method == "POST":
        ing_id = request.POST.get("ingredient")
        actual_qty_str = request.POST.get("actual_quantity", "").strip()
        note = request.POST.get("note", "").strip()
        if not ing_id:
            error_message = "Please select an ingredient."
        else:
            try:
                ing = ingredients.get(id=ing_id)
                actual_qty = Decimal(actual_qty_str)
                stock_count = record_stock_count_audit(
                    ingredient=ing,
                    actual_quantity=actual_qty,
                    note=note,
                    user=request.user if request.user.is_authenticated else None,
                )
                messages.success(request, f"Stock count audit for '{ing.name}' saved. Variance: {stock_count.variance:+} {ing.cost_unit}.")
                return redirect("stock_count_list")
            except (ValidationError, InvalidOperation, ValueError) as e:
                error_message = " ".join(e.messages) if hasattr(e, "messages") else str(e)
            except Ingredient.DoesNotExist:
                error_message = "Selected ingredient not found."
    return render(request, "inventory/stock_count_form.html", {
        "ingredients": ingredients,
        "selected_ingredient_id": selected_ingredient_id,
        "error_message": error_message,
    })


# =========================================================
# STOCK TRANSACTIONS
# =========================================================

@login_required(login_url='/auth/login/')
@inventory_staff_required
def stock_transaction_list(request):
    restaurant = getattr(request.user, "restaurant", None) or Restaurant.objects.first()
    transactions = StockTransaction.objects.select_related("ingredient", "ingredient__category", "order").order_by("-created_at", "-id")
    if restaurant:
        transactions = transactions.filter(ingredient__restaurant=restaurant)

    # Filter by ingredient
    ing_id = request.GET.get("ingredient")
    if ing_id:
        transactions = transactions.filter(ingredient_id=ing_id)

    # Filter by transaction_type
    tx_type = request.GET.get("type")
    if tx_type:
        transactions = transactions.filter(transaction_type=tx_type)

    ingredients = Ingredient.objects.filter(is_active=True).order_by("name")
    if restaurant:
        ingredients = ingredients.filter(restaurant=restaurant)

    return render(request, "inventory/transaction_list.html", {
        "transactions": transactions[:200],
        "ingredients": ingredients,
        "selected_ingredient": ing_id,
        "selected_type": tx_type,
        "types": StockTransaction.TransactionType.choices,
    })


# =========================================================
# INGREDIENT REQUESTS (OWNER / MANAGER REVIEW)
# =========================================================

@login_required(login_url="/auth/login/")
def ingredient_request_list(request):
    user = request.user
    if not user.is_superuser and user.role not in ["admin", "owner", "manager"]:
        raise PermissionDenied("Only managers and owners can manage ingredient requests.")

    restaurant = getattr(user, "restaurant", None) or Restaurant.objects.first()
    from restaurant.branch_services import get_active_branch
    active_branch = get_active_branch(request, restaurant) if restaurant else None

    requests_qs = IngredientRequest.objects.select_related(
        "ingredient", "ingredient__category", "branch", "restaurant", "requested_by", "reviewed_by", "purchase_order"
    ).order_by("-created_at")

    if restaurant:
        requests_qs = requests_qs.filter(restaurant=restaurant)
    if active_branch:
        requests_qs = requests_qs.filter(branch=active_branch)

    status_filter = request.GET.get("status", "").strip()
    if status_filter:
        requests_qs = requests_qs.filter(status=status_filter)

    priority_filter = request.GET.get("priority", "").strip()
    if priority_filter:
        requests_qs = requests_qs.filter(priority=priority_filter)

    suppliers = Supplier.objects.filter(is_active=True)
    if restaurant:
        suppliers = suppliers.filter(restaurant=restaurant)

    return render(request, "inventory/ingredient_requests.html", {
        "requests": requests_qs,
        "selected_status": status_filter,
        "selected_priority": priority_filter,
        "statuses": IngredientRequest.Status.choices,
        "priorities": IngredientRequest.Priority.choices,
        "active_branch": active_branch,
        "suppliers": suppliers,
    })


@login_required(login_url="/auth/login/")
def approve_ingredient_request(request, request_id):
    user = request.user
    if not user.is_superuser and user.role not in ["admin", "owner", "manager"]:
        raise PermissionDenied("Only managers and owners can approve ingredient requests.")

    req = get_object_or_404(IngredientRequest, pk=request_id)
    if req.status != IngredientRequest.Status.PENDING:
        messages.warning(request, f"Request #{req.id} is not pending approval.")
    else:
        req.status = IngredientRequest.Status.APPROVED
        req.reviewed_by = user
        req.reviewed_at = timezone.now()
        req.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
        messages.success(request, f"Request #{req.id} for {req.ingredient.name} approved.")

    next_url = request.POST.get("next") or request.GET.get("next") or "/inventory/requests/"
    return redirect(next_url)


@login_required(login_url="/auth/login/")
def reject_ingredient_request(request, request_id):
    user = request.user
    if not user.is_superuser and user.role not in ["admin", "owner", "manager"]:
        raise PermissionDenied("Only managers and owners can reject ingredient requests.")

    req = get_object_or_404(IngredientRequest, pk=request_id)
    if req.status not in [IngredientRequest.Status.PENDING, IngredientRequest.Status.APPROVED]:
        messages.warning(request, f"Request #{req.id} cannot be rejected in status '{req.status}'.")
    else:
        reason = request.POST.get("rejection_reason", "").strip()
        req.status = IngredientRequest.Status.REJECTED
        req.reviewed_by = user
        req.reviewed_at = timezone.now()
        req.rejection_reason = reason
        req.save(update_fields=["status", "reviewed_by", "reviewed_at", "rejection_reason", "updated_at"])
        messages.info(request, f"Request #{req.id} for {req.ingredient.name} rejected.")

    next_url = request.POST.get("next") or request.GET.get("next") or "/inventory/requests/"
    return redirect(next_url)


@login_required(login_url="/auth/login/")
def convert_request_to_po(request, request_id):
    user = request.user
    if not user.is_superuser and user.role not in ["admin", "owner", "manager"]:
        raise PermissionDenied("Only managers and owners can convert requests to purchase orders.")

    req = get_object_or_404(IngredientRequest, pk=request_id)
    if req.status != IngredientRequest.Status.APPROVED:
        messages.error(request, f"Request #{req.id} must be Approved before converting to a Purchase Order.")
        return redirect("/inventory/requests/")

    restaurant = req.restaurant
    suppliers = Supplier.objects.filter(restaurant=restaurant, is_active=True).order_by("name")

    if request.method == "POST":
        supplier_id = request.POST.get("supplier_id")
        pack_price_raw = request.POST.get("pack_price", "").strip()
        invoice_number = request.POST.get("invoice_number", "").strip()
        purchase_date = request.POST.get("purchase_date") or timezone.now().date()

        supplier = suppliers.filter(id=supplier_id).first()
        if not supplier:
            messages.error(request, "Please select a valid supplier.")
            return redirect(request.POST.get("next") or "/inventory/requests/")

        try:
            pack_price = Decimal(pack_price_raw) if pack_price_raw else req.ingredient.current_pack_price
            if pack_price < 0:
                pack_price = Decimal("0.00")
        except (InvalidOperation, ValueError, TypeError):
            pack_price = req.ingredient.current_pack_price

        pack_size = req.ingredient.pack_size or Decimal("1.000")
        pack_quantity = (req.quantity / pack_size).quantize(Decimal("0.001"))
        if pack_quantity <= 0:
            pack_quantity = Decimal("1.000")

        total_price = pack_quantity * pack_price

        with transaction.atomic():
            po = PurchaseOrder.objects.create(
                restaurant=restaurant,
                branch=req.branch,
                supplier=supplier,
                invoice_number=invoice_number,
                purchase_date=purchase_date,
                status=PurchaseOrder.Status.DRAFT,
                total_amount=total_price,
                note=f"Created from Stock Request #{req.id}: {req.reason}",
            )
            PurchaseOrderItem.objects.create(
                purchase_order=po,
                ingredient=req.ingredient,
                pack_quantity=pack_quantity,
                pack_price=pack_price,
                total_price=total_price,
            )
            req.purchase_order = po
            req.status = IngredientRequest.Status.ORDERED
            req.save(update_fields=["purchase_order", "status", "updated_at"])

        messages.success(request, f"Request #{req.id} converted to Purchase Order #{po.id}.")
        return redirect("purchase_detail", purchase_id=po.id)

    messages.error(request, "Invalid conversion method.")
    return redirect("/inventory/requests/")