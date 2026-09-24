from decimal import Decimal, ROUND_CEILING

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from inventory.models import (
    Ingredient,
    IngredientPriceHistory,
    PurchaseOrder,
    PurchaseOrderItem,
    StockCount,
    StockReservation,
    StockTransaction,
    WasteRecord,
)
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
    """Aggregate all order items and their selected addons before rounding to inventory precision."""
    requirements = {}
    items_qs = order.items.select_related("menu_item__category").prefetch_related(
        "addons__addon_option__group",
        "addons__addon_option__ingredient_requirements__ingredient",
    )
    for item in items_qs:
        if item.menu_item.category.restaurant_id != order.restaurant_id:
            raise ValidationError("Order menu items belong to another restaurant.")
        if not item.menu_item.is_available:
            raise ValidationError(f"Menu item '{item.menu_item.name}' is unavailable.")
        merge_requirements(
            requirements, _menu_item_requirements(item.menu_item, item.quantity),
        )

        item_qty = to_decimal(item.quantity)
        for order_addon in item.addons.all():
            addon_opt = order_addon.addon_option
            if addon_opt:
                if addon_opt.group.restaurant_id != order.restaurant_id:
                    raise ValidationError("Addon option belongs to another restaurant.")
                for addon_req in addon_opt.ingredient_requirements.all():
                    ing = addon_req.ingredient
                    if ing.restaurant_id != order.restaurant_id:
                        raise ValidationError(f"Addon ingredient '{ing.name}' belongs to another restaurant.")
                    if not ing.is_active:
                        raise ValidationError(f"Addon ingredient '{ing.name}' is inactive.")
                    req_qty = to_decimal(addon_req.quantity)
                    if not req_qty.is_finite() or req_qty <= 0:
                        raise ValidationError(f"Invalid addon ingredient quantity for '{ing.name}'.")
                    merge_requirements(requirements, {
                        ing.pk: {
                            "ingredient": ing,
                            "required_quantity": req_qty * item_qty,
                        }
                    })

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
                unit_cost_snapshot=ingredient.current_unit_cost,
                order=order,
                note=f"Consumed for Order #{order.pk}",
            )
        reservation.status = StockReservation.Status.CONSUMED
        reservation.save(update_fields=["status"])
    return len(pending)


def calculate_item_available_portions(menu_item):
    """Calculate maximum portions that can be prepared from available stock.

    Returns an integer portion count.
    If a normal item has no recipe, returns 9999 (not constrained by ingredient stock).
    If a set menu has no components, returns 0.
    """
    if menu_item.item_type == MenuItem.ItemType.SET_MENU:
        if not menu_item.set_components.exists():
            return 0
    elif not hasattr(menu_item, "recipe") or not menu_item.recipe.recipe_ingredients.exists():
        return 9999

    try:
        requirements = get_menu_item_requirements(menu_item, quantity=1)
    except ValidationError:
        return 0

    if not requirements:
        return 0

    portions = []
    for row in requirements.values():
        ingredient = row["ingredient"]
        if not ingredient.is_active:
            return 0
        req_qty = row["required_quantity"]
        if req_qty <= 0:
            continue
        avail = ingredient.available_stock
        if avail <= 0:
            return 0
        portions.append(int(avail // req_qty))

    return min(portions) if portions else 0


def calculate_addon_available_portions(addon_option):
    """Calculate maximum portions that can be served for this addon option from available stock.

    Returns an integer portion count.
    If the addon option has no ingredient requirements, returns 9999 (not constrained by ingredient stock).
    If it requires ingredients, returns min(available_stock // req_qty) across all requirements.
    If any required ingredient is inactive or has insufficient stock, returns 0.
    """
    reqs = list(addon_option.ingredient_requirements.select_related("ingredient").all())
    if not reqs:
        return 9999

    portions = []
    for row in reqs:
        ingredient = row.ingredient
        if not ingredient.is_active:
            return 0
        req_qty = row.quantity
        if req_qty <= 0:
            continue
        avail = ingredient.available_stock
        if avail <= 0:
            return 0
        portions.append(int(avail // req_qty))

    return min(portions) if portions else 0


@transaction.atomic
def record_stock_purchase(purchase_order, user=None):
    """Atomically receive a purchase order, increase physical stock, and log transactions.

    Guards against duplicate receiving and updates ingredient pack prices and price histories.
    """
    locked_po = PurchaseOrder.objects.select_for_update().get(pk=purchase_order.pk)
    if locked_po.status == PurchaseOrder.Status.RECEIVED:
        raise ValidationError("This purchase order has already been received.")

    items = list(locked_po.items.select_related("ingredient").all())
    if not items:
        raise ValidationError("Cannot receive an empty purchase order.")

    # Sort ingredient IDs to avoid deadlocks
    ingredient_ids = sorted([item.ingredient_id for item in items])
    ingredient_map = _lock_ingredients(ingredient_ids)

    for item in items:
        ingredient = ingredient_map[item.ingredient_id]
        if ingredient.restaurant_id != locked_po.restaurant_id:
            raise ValidationError(f"Ingredient '{ingredient.name}' belongs to another restaurant.")

        base_qty = (item.pack_quantity * ingredient.pack_size).quantize(STOCK_PRECISION)
        ingredient.current_stock += base_qty

        if item.pack_price > 0:
            ingredient.current_pack_price = item.pack_price
            IngredientPriceHistory.objects.create(
                ingredient=ingredient,
                pack_price=item.pack_price,
                effective_date=locked_po.purchase_date,
            )

        ingredient.save(update_fields=["current_stock", "current_pack_price", "updated_at"])

        invoice_label = f" (Invoice: {locked_po.invoice_number})" if locked_po.invoice_number else ""
        StockTransaction.objects.create(
            ingredient=ingredient,
            transaction_type=StockTransaction.TransactionType.PURCHASE,
            quantity=base_qty,
            note=f"PO #{locked_po.pk}{invoice_label}",
        )

    locked_po.status = PurchaseOrder.Status.RECEIVED
    locked_po.received_at = timezone.now()
    locked_po.received_by = user
    locked_po.save(update_fields=["status", "received_at", "received_by", "updated_at"])

    # Update any linked ingredient requests
    from .models import IngredientRequest
    locked_po.converted_requests.filter(
        status__in=[IngredientRequest.Status.ORDERED, IngredientRequest.Status.APPROVED]
    ).update(status=IngredientRequest.Status.RECEIVED)

    return locked_po


@transaction.atomic
def record_stock_waste(ingredient, quantity, reason, note="", order=None, user=None):
    """Atomically record inventory waste, decrement stock, and log WasteRecord & StockTransaction."""
    quantity = to_decimal(quantity).quantize(STOCK_PRECISION)
    if quantity <= 0:
        raise ValidationError("Waste quantity must be greater than zero.")

    valid_reasons = {choice for choice, _ in WasteRecord.Reason.choices}
    if reason not in valid_reasons:
        raise ValidationError(f"Invalid waste reason: {reason}")

    locked_ing = Ingredient.objects.select_for_update().get(pk=ingredient.pk)
    if locked_ing.current_stock < quantity:
        raise ValidationError(
            f"Cannot waste {quantity} {locked_ing.base_unit}; physical stock is {locked_ing.current_stock} {locked_ing.base_unit}."
        )

    if (locked_ing.current_stock - quantity) < locked_ing.reserved_stock:
        raise ValidationError(
            f"Cannot waste {quantity} {locked_ing.base_unit}; physical stock would drop below active reservations ({locked_ing.reserved_stock} {locked_ing.base_unit})."
        )

    locked_ing.current_stock -= quantity
    locked_ing.save(update_fields=["current_stock", "updated_at"])

    waste_record = WasteRecord.objects.create(
        ingredient=locked_ing,
        quantity=quantity,
        reason=reason,
        unit_cost_snapshot=locked_ing.current_unit_cost,
        order=order,
        note=note,
    )

    reason_label = dict(WasteRecord.Reason.choices).get(reason, reason)
    note_text = f"Waste ({reason_label}): {note}".strip(" :")
    StockTransaction.objects.create(
        ingredient=locked_ing,
        transaction_type=StockTransaction.TransactionType.WASTE,
        quantity=quantity,
        unit_cost_snapshot=locked_ing.current_unit_cost,
        order=order,
        note=note_text,
    )
    return waste_record


@transaction.atomic
def record_stock_count_audit(ingredient, actual_quantity, note="", user=None):
    """Atomically record a physical stock audit, reconcile stock variance, and log adjustment transactions."""
    actual_quantity = to_decimal(actual_quantity).quantize(STOCK_PRECISION)
    if actual_quantity < 0:
        raise ValidationError("Actual quantity cannot be negative.")

    locked_ing = Ingredient.objects.select_for_update().get(pk=ingredient.pk)
    if actual_quantity < locked_ing.reserved_stock:
        raise ValidationError(
            f"Counted physical stock ({actual_quantity} {locked_ing.base_unit}) cannot be lower than active order reservations ({locked_ing.reserved_stock} {locked_ing.base_unit})."
        )

    system_quantity = locked_ing.current_stock
    variance = (actual_quantity - system_quantity).quantize(STOCK_PRECISION)

    locked_ing.current_stock = actual_quantity
    locked_ing.save(update_fields=["current_stock", "updated_at"])

    stock_count = StockCount.objects.create(
        ingredient=locked_ing,
        system_quantity=system_quantity,
        actual_quantity=actual_quantity,
        note=note,
    )

    if variance > 0:
        StockTransaction.objects.create(
            ingredient=locked_ing,
            transaction_type=StockTransaction.TransactionType.ADJUSTMENT_IN,
            quantity=variance,
            unit_cost_snapshot=locked_ing.current_unit_cost,
            note=f"Audit variance (+{variance}): {note}".strip(" :"),
        )
    elif variance < 0:
        StockTransaction.objects.create(
            ingredient=locked_ing,
            transaction_type=StockTransaction.TransactionType.ADJUSTMENT_OUT,
            quantity=abs(variance),
            unit_cost_snapshot=locked_ing.current_unit_cost,
            note=f"Audit variance ({variance}): {note}".strip(" :"),
        )

    return stock_count


@transaction.atomic
def record_opening_stock(
    ingredient,
    quantity=None,
    unit_cost=None,
    timestamp=None,
    note="Initial opening balance / imported stock",
    user=None,
):
    """
    Record an OPENING_BALANCE stock transaction for an ingredient.
    Does NOT increase or modify current_stock (historical baseline only).
    """
    if quantity is None:
        quantity = ingredient.current_stock
    quantity = to_decimal(quantity).quantize(STOCK_PRECISION)
    if quantity <= 0:
        return None

    if unit_cost is None:
        unit_cost = ingredient.current_unit_cost
    unit_cost = to_decimal(unit_cost)

    tx_kwargs = {
        "ingredient": ingredient,
        "restaurant": ingredient.restaurant,
        "transaction_type": StockTransaction.TransactionType.OPENING_BALANCE,
        "quantity": quantity,
        "unit_cost_snapshot": unit_cost,
        "note": note,
    }
    if timestamp:
        tx_kwargs["created_at"] = timestamp

    return StockTransaction.objects.create(**tx_kwargs)


@transaction.atomic
def backfill_opening_stock_transactions():
    """
    Idempotent backfill:
    For existing ingredients with current_stock > 0 but NO historical
    OPENING_BALANCE or PURCHASE transaction, create one OPENING_BALANCE transaction.
    Does NOT modify current_stock.
    """
    has_opening_or_purchase = StockTransaction.objects.filter(
        transaction_type__in=[
            StockTransaction.TransactionType.OPENING_BALANCE,
            StockTransaction.TransactionType.PURCHASE,
        ]
    ).values_list("ingredient_id", flat=True).distinct()

    candidates = (
        Ingredient.objects.filter(current_stock__gt=Decimal("0.000"))
        .exclude(id__in=has_opening_or_purchase)
        .select_related("restaurant")
        .order_by("id")
    )

    created_txns = []
    for ing in candidates:
        txn = record_opening_stock(
            ingredient=ing,
            quantity=ing.current_stock,
            unit_cost=ing.current_unit_cost,
            timestamp=ing.created_at or timezone.now(),
            note="Initial opening balance / imported stock",
        )
        if txn:
            created_txns.append(txn)

    return created_txns

