from decimal import Decimal
from django.db import migrations


def backfill_opening_stock(apps, schema_editor):
    Ingredient = apps.get_model("inventory", "Ingredient")
    StockTransaction = apps.get_model("inventory", "StockTransaction")

    # Populate restaurant on existing transactions where missing
    for tx in StockTransaction.objects.filter(restaurant__isnull=True).select_related("ingredient"):
        tx.restaurant_id = tx.ingredient.restaurant_id
        tx.save(update_fields=["restaurant"])

    # Find ingredients with existing opening balance or purchase transactions
    has_opening_or_purchase = set(
        StockTransaction.objects.filter(
            transaction_type__in=["OPENING_BALANCE", "PURCHASE"]
        ).values_list("ingredient_id", flat=True).distinct()
    )

    candidates = (
        Ingredient.objects.filter(current_stock__gt=Decimal("0.000"))
        .exclude(id__in=has_opening_or_purchase)
        .select_related("restaurant")
    )

    for ing in candidates:
        unit_cost = (
            (ing.current_pack_price / ing.pack_size)
            if ing.pack_size and ing.pack_size > 0
            else Decimal("0.000000")
        )
        StockTransaction.objects.create(
            ingredient=ing,
            restaurant=ing.restaurant,
            transaction_type="OPENING_BALANCE",
            quantity=ing.current_stock,
            unit_cost_snapshot=unit_cost,
            created_at=ing.created_at,
            note="Initial opening balance / imported stock",
        )


def reverse_backfill(apps, schema_editor):
    StockTransaction = apps.get_model("inventory", "StockTransaction")
    StockTransaction.objects.filter(
        transaction_type="OPENING_BALANCE",
        note="Initial opening balance / imported stock",
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0005_stocktransaction_restaurant_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_opening_stock, reverse_backfill),
    ]
