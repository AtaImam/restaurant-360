from decimal import Decimal, ROUND_CEILING

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction
from django.db.models import Sum

from inventory.models import Ingredient, StockReservation, StockTransaction
from menu.models import MenuItem
from orders.models import Order


STOCK_PRECISION = Decimal("0.001")
RESERVABLE_STATUSES = {"NEW", "ACCEPTED"}
CONSUMABLE_STATUSES = {"ACCEPTED", "PREPARING", "READY", "SERVED", "COMPLETED"}


def to_decimal(value):
    return value if isinstance(value, Decimal) else Decimal(str(value))


def merge_requirements(target, source):
    """Combine quantities in the ingredient's existing base unit."""
    for ingredient_id, data in source.items():
        if ingredient_id not in target:
            target[ingredient_id] = {
                "ingredient": data["ingredient"],
                "required_quantity": Decimal("0"),
            }
        target[ingredient_id]["required_quantity"] += data["required_quantity"]


def _quantize_requirements(requirements):
    # Round once after aggregation. Fractional recipe yields must never reserve
    # zero, or deduct a different quantity from the three-decimal ledger.
    for data in requirements.values():
        data["required_quantity"] = data["required_quantity"].quantize(
            STOCK_PRECISION, rounding=ROUND_CEILING,
        )
    return requirements


def _menu_item_requirements(menu_item, quantity):
    quantity = to_decimal(quantity)
    if not quantity.is_finite() or quantity <= 0:
        raise ValidationError(f"Invalid quantity for '{menu_item.name}'.")

    restaurant_id = menu_item.category.restaurant_id
    requirements = {}
    if menu_item.item_type == MenuItem.ItemType.SET_MENU:
        components = list(
            menu_item.set_components.select_related("component__category")
        )
        if not components:
            raise ValidationError(f"Set menu '{menu_item.name}' has no components.")

        for component_row in components:
            component = component_row.component
            if component.item_type != MenuItem.ItemType.NORMAL:
                raise ValidationError("A set menu can contain only normal menu items.")
            if component.category.restaurant_id != restaurant_id:
                raise ValidationError("Set menu components belong to another restaurant.")
            if not component.is_available:
                raise ValidationError(f"Set menu component '{component.name}' is unavailable.")
            component_quantity = to_decimal(component_row.quantity)
            if not component_quantity.is_finite() or component_quantity <= 0:
                raise ValidationError(f"Invalid set menu quantity for '{component.name}'.")
            merge_requirements(
                requirements,
                _menu_item_requirements(component, quantity * component_quantity),
            )
        # Set menus deliberately use component recipes, never a direct recipe.
        return requirements

    if menu_item.item_type != MenuItem.ItemType.NORMAL:
        raise ValidationError(f"Invalid menu item type for '{menu_item.name}'.")
    try:
        recipe = menu_item.recipe
    except ObjectDoesNotExist:
        raise ValidationError(f"No recipe found for '{menu_item.name}'.")

    yield_quantity = to_decimal(recipe.yield_quantity)
    if not yield_quantity.is_finite() or yield_quantity <= 0:
        raise ValidationError(f"Invalid recipe yield for '{menu_item.name}'.")
    recipe_rows = list(recipe.recipe_ingredients.select_related("ingredient"))
    if not recipe_rows:
        raise ValidationError(f"Recipe for '{menu_item.name}' has no ingredients.")

    for row in recipe_rows:
        ingredient = row.ingredient
        if ingredient.restaurant_id != restaurant_id:
            raise ValidationError(f"Ingredient '{ingredient.name}' belongs to another restaurant.")
        if not ingredient.is_active:
            raise ValidationError(f"Ingredient '{ingredient.name}' is inactive.")
        row_quantity = to_decimal(row.quantity)
        if not row_quantity.is_finite() or row_quantity <= 0:
            raise ValidationError(f"Invalid recipe quantity for '{ingredient.name}'.")
        merge_requirements(requirements, {
            ingredient.pk: {
                "ingredient": ingredient,
                "required_quantity": row_quantity * quantity / yield_quantity,
            },
        })
    return requirements


def get_menu_item_requirements(menu_item, quantity=1):
    """Calculate normal or component-based set requirements without changing stock."""
    return _quantize_requirements(_menu_item_requirements(menu_item, quantity))


def get_order_requirements(order):
    """Aggregate all order items before rounding to inventory precision."""
    requirements = {}
    for item in order.items.select_related("menu_item__category"):
        if item.menu_item.category.restaurant_id != order.restaurant_id:
            raise ValidationError("Order menu items belong to another restaurant.")
        if not item.menu_item.is_available:
            raise ValidationError(f"Menu item '{item.menu_item.name}' is unavailable.")
        merge_requirements(
            requirements, _menu_item_requirements(item.menu_item, item.quantity),
        )
    return _quantize_requirements(requirements)


def _get_other_active_reservations(order, ingredient_ids):
    rows = (
        StockReservation.objects.filter(
            ingredient_id__in=ingredient_ids,
            status=StockReservation.Status.ACTIVE,
        )
        .exclude(order=order)
        .values("ingredient_id")
        .annotate(total=Sum("quantity"))
    )
    return {row["ingredient_id"]: to_decimal(row["total"] or 0) for row in rows}


def _stock_shortages(order, requirements, ingredient_map):
    other_reserved = _get_other_active_reservations(order, requirements)
    shortages = []
    for ingredient_id, data in requirements.items():
        ingredient = ingredient_map.get(ingredient_id)
        if ingredient is None:
            raise ValidationError(f"Ingredient ID {ingredient_id} does not exist.")
        _validate_ingredient_restaurant(order, ingredient)
        if not ingredient.is_active:
            raise ValidationError(f"Ingredient '{ingredient.name}' is inactive.")
        required = data["required_quantity"]
        available = ingredient.current_stock - other_reserved.get(ingredient_id, Decimal("0"))
        if available < required:
            shortages.append({
                "ingredient": ingredient,
                "required": required,
                "available": max(available, Decimal("0")),
                "shortage": max(required - available, Decimal("0")),
            })
    return shortages


def check_order_stock(order):
    """Read-only availability hint; reservation repeats validation under locks."""
    requirements = get_order_requirements(order)
    ingredients = Ingredient.objects.filter(pk__in=requirements)
    return _stock_shortages(order, requirements, {row.pk: row for row in ingredients})


def _lock_order(order):
    if not order.pk:
        raise ValidationError("Save the order before updating its inventory.")
    return Order.objects.select_for_update().get(pk=order.pk)


def _lock_ingredients(ingredient_ids):
    # All inventory operations use Order -> sorted Ingredients -> Reservations.
    # The Ingredient lock serializes availability checks across different orders.
    ingredients = Ingredient.objects.select_for_update().filter(
        pk__in=ingredient_ids,
    ).order_by("pk")
    return {ingredient.pk: ingredient for ingredient in ingredients}


def _lock_reservations(order):
    return list(
        StockReservation.objects.select_for_update().filter(order=order)
        .order_by("ingredient_id")
    )


def _validate_ingredient_restaurant(order, ingredient):
    if ingredient.restaurant_id != order.restaurant_id:
        raise ValidationError(f"Ingredient '{ingredient.name}' belongs to another restaurant.")


def _consumption_transactions(order):
    return StockTransaction.objects.filter(
        order=order,
        transaction_type=StockTransaction.TransactionType.CONSUMPTION,
    )


@transaction.atomic
def reserve_stock_for_order(order):
    """Sync the single reservation per ingredient without changing physical stock."""
    order = _lock_order(order)
    if order.status not in RESERVABLE_STATUSES:
        raise ValidationError("Only NEW or ACCEPTED orders can reserve stock.")
    requirements = get_order_requirements(order)
    if not requirements:
        raise ValidationError("Order has no recipe ingredients to reserve.")

    existing_ids = set(order.stock_reservations.values_list("ingredient_id", flat=True))
    ingredient_map = _lock_ingredients(existing_ids | set(requirements))
    reservations = _lock_reservations(order)
    if any(row.status == StockReservation.Status.CONSUMED for row in reservations):
        raise ValidationError("Consumed order inventory cannot be reserved again.")
    if _consumption_transactions(order).exists():
        raise ValidationError("This order already has consumption transactions; reconcile its inventory.")

    shortages = _stock_shortages(order, requirements, ingredient_map)
    if shortages:
        details = "; ".join(
            f"{row['ingredient'].name}: required {row['required']} "
            f"{row['ingredient'].base_unit}, available {row['available']} "
            f"{row['ingredient'].base_unit}"
            for row in shortages
        )
        raise ValidationError("Insufficient stock — " + details)

    existing = {row.ingredient_id: row for row in reservations}
    for row in reservations:
        if row.status not in {StockReservation.Status.ACTIVE, StockReservation.Status.RELEASED}:
            raise ValidationError(f"Invalid reservation status for Order #{order.pk}.")
        if row.ingredient_id not in requirements and row.status == StockReservation.Status.ACTIVE:
            row.status = StockReservation.Status.RELEASED
            row.save(update_fields=["status"])

    active = []
    for ingredient_id in sorted(requirements):
        quantity = requirements[ingredient_id]["required_quantity"]
        row = existing.get(ingredient_id)
        if row is None:
            row = StockReservation.objects.create(
                order=order, ingredient=ingredient_map[ingredient_id],
                quantity=quantity, status=StockReservation.Status.ACTIVE,
            )
        else:
            row.quantity = quantity
            row.status = StockReservation.Status.ACTIVE
            row.save(update_fields=["quantity", "status"])
        active.append(row)
    return active


@transaction.atomic
def release_order_reservations(order):
    """Release unconsumed inventory; this never restores physical stock."""
    order = _lock_order(order)
    if order.status not in RESERVABLE_STATUSES:
        raise ValidationError("Only NEW or ACCEPTED orders can release inventory reservations.")
    _lock_ingredients(set(order.stock_reservations.values_list("ingredient_id", flat=True)))
    reservations = _lock_reservations(order)
    if any(row.status == StockReservation.Status.CONSUMED for row in reservations):
        raise ValidationError("Consumed inventory requires an explicit waste, return, or adjustment.")
    if _consumption_transactions(order).exists():
        raise ValidationError("This order has consumption transactions and cannot release inventory.")
    return StockReservation.objects.filter(
        order=order, status=StockReservation.Status.ACTIVE,
    ).update(status=StockReservation.Status.RELEASED)


@transaction.atomic
def consume_order_reservations(order, *, dry_run=False):
    """Consume exactly once, or reconcile an exact existing consumption ledger.

    Call inside the order status transition transaction. Historical advanced orders
    are also supported. Ambiguous history fails the entire order without guessing.
    dry_run performs the same validation and locking but makes no changes.
    """
    order = _lock_order(order)
    if order.status not in CONSUMABLE_STATUSES:
        raise ValidationError("Only ACCEPTED or later orders can consume reserved inventory.")

    ingredient_ids = set(order.stock_reservations.values_list("ingredient_id", flat=True))
    ingredient_map = _lock_ingredients(ingredient_ids)
    reservations = _lock_reservations(order)
    if not reservations:
        raise ValidationError(f"Order #{order.pk} has no inventory reservations; review its inventory before preparing.")

    ledger = {}
    for entry in _consumption_transactions(order).order_by("ingredient_id", "pk"):
        ledger.setdefault(entry.ingredient_id, []).append(entry)
    if set(ledger) - ingredient_ids:
        raise ValidationError(f"Order #{order.pk} has consumption without a matching reservation; manual review required.")

    pending = []
    has_consumed = False
    for reservation in reservations:
        ingredient = ingredient_map.get(reservation.ingredient_id)
        if ingredient is None:
            raise ValidationError("Reserved ingredient does not exist.")
        _validate_ingredient_restaurant(order, ingredient)
        quantity = to_decimal(reservation.quantity)
        if not quantity.is_finite() or quantity <= 0:
            raise ValidationError(f"Invalid reserved quantity for '{ingredient.name}'.")

        entries = ledger.get(ingredient.pk, [])
        if entries and (len(entries) != 1 or entries[0].quantity != quantity):
            raise ValidationError(
                f"Order #{order.pk}, '{ingredient.name}': ambiguous consumption ledger "
                f"(expected one transaction for {quantity} {ingredient.base_unit}); manual review required."
            )
        if reservation.status == StockReservation.Status.CONSUMED:
            if not entries:
                raise ValidationError(
                    f"Order #{order.pk}, '{ingredient.name}': CONSUMED reservation has no "
                    "matching consumption transaction; manual review required."
                )
            has_consumed = True
            continue
        if reservation.status == StockReservation.Status.RELEASED:
            if entries:
                raise ValidationError(
                    f"Order #{order.pk}, '{ingredient.name}': RELEASED reservation has "
                    "consumption transactions; manual review required."
                )
            continue
        if reservation.status != StockReservation.Status.ACTIVE:
            raise ValidationError(f"Invalid reservation status for Order #{order.pk}.")
        if not entries and ingredient.current_stock < quantity:
            raise ValidationError(
                f"Stock mismatch for '{ingredient.name}': reserved {quantity} "
                f"{ingredient.base_unit}, but physical stock is {ingredient.current_stock} "
                f"{ingredient.base_unit}."
            )
        pending.append((reservation, ingredient, bool(entries)))

    if not pending and not has_consumed:
        raise ValidationError(f"Order #{order.pk} has no ACTIVE or valid CONSUMED reservations; reserve inventory first.")
    if dry_run:
        return len(pending)

    # All rows are validated before the first write. An existing exact ledger is
    # evidence of a prior deduction, so only repair its reservation status.
    for reservation, ingredient, already_deducted in pending:
        if not already_deducted:
            ingredient.current_stock -= reservation.quantity
            ingredient.save(update_fields=["current_stock", "updated_at"])
            StockTransaction.objects.create(
                ingredient=ingredient,
                transaction_type=StockTransaction.TransactionType.CONSUMPTION,
                quantity=reservation.quantity,
                order=order,
                note=f"Consumed for Order #{order.pk}",
            )
        reservation.status = StockReservation.Status.CONSUMED
        reservation.save(update_fields=["status"])
    return len(pending)
