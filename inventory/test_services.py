"""Verify reservation, consumption and historical repair against the database."""

from decimal import Decimal
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.urls import reverse

from inventory.models import Ingredient, IngredientCategory, Recipe, RecipeIngredient, StockReservation, StockTransaction
from inventory.services import (
    consume_order_reservations,
    get_menu_item_requirements,
    get_order_requirements,
    release_order_reservations,
    reserve_stock_for_order,
)
from menu.models import Category, MenuItem, SetMenuComponent
from orders.models import Order
from orders.services import transition_order_status
from orders.test_flow import OrderFlowFixture


class RequirementTests(OrderFlowFixture, TestCase):
    def test_normal_recipe_yield_and_shared_ingredients_are_aggregated(self):
        order = self.create_order(reserve=False)
        requirements = get_order_requirements(order)
        self.assertEqual(set(requirements), {self.chicken.pk, self.salt.pk})
        self.assertEqual(requirements[self.chicken.pk]["required_quantity"], Decimal("250"))
        self.assertEqual(requirements[self.salt.pk]["required_quantity"], Decimal("5"))
        self.assertFalse(StockReservation.objects.exists())
        reserve_stock_for_order(order)
        self.assert_inventory(order)

    def test_set_menu_uses_component_quantities_yields_and_order_quantity(self):
        # A legacy direct recipe must never replace component calculations.
        fake_recipe = Recipe.objects.create(menu_item=self.set_menu)
        RecipeIngredient.objects.create(recipe=fake_recipe, ingredient=self.chicken, quantity=999)
        order = self.create_order(items=[(self.set_menu, 2)])
        self.assert_inventory(order, chicken="500", salt="10")

    def test_set_and_normal_item_shared_ingredients_are_combined(self):
        order = self.create_order(items=[(self.set_menu, 1), (self.item_a, 1)])
        self.assert_inventory(order, chicken="350", salt="7")

    def test_fractional_yield_rounds_up_once_after_all_order_lines_are_aggregated(self):
        Recipe.objects.filter(menu_item=self.item_a).update(yield_quantity=3)
        RecipeIngredient.objects.filter(recipe__menu_item=self.item_a, ingredient=self.chicken).update(quantity=1)
        single = get_menu_item_requirements(MenuItem.objects.get(pk=self.item_a.pk))
        self.assertEqual(single[self.chicken.pk]["required_quantity"], Decimal("0.334"))
        order = self.create_order(items=[(self.item_a, 1), (self.item_a, 2)])
        self.assert_inventory(order, chicken="1", salt="4")
        transition_order_status(order, "ACCEPTED")
        transition_order_status(order, "PREPARING")
        self.assert_inventory(order, consumed=True, chicken="1", salt="4")

    def test_missing_recipe_empty_recipe_and_empty_set_are_rejected(self):
        missing = MenuItem.objects.create(category=self.category, name="No recipe", price=10)
        empty = MenuItem.objects.create(category=self.category, name="Empty recipe", price=10)
        Recipe.objects.create(menu_item=empty)
        empty_set = MenuItem.objects.create(
            category=self.category, name="Empty set", price=10, item_type=MenuItem.ItemType.SET_MENU
        )
        for item in [missing, empty, empty_set]:
            with self.subTest(item=item.name), self.assertRaises(ValidationError):
                get_menu_item_requirements(item)

    def test_invalid_recipe_yield_cannot_create_reservations(self):
        Recipe.objects.filter(menu_item=self.item_a).update(yield_quantity=0)
        order = self.create_order(reserve=False)
        with self.assertRaises(ValidationError):
            reserve_stock_for_order(order)
        self.assertFalse(order.stock_reservations.exists())

    def foreign_ingredient(self):
        category = IngredientCategory.objects.create(restaurant=self.other_restaurant, name="Other kitchen")
        return Ingredient.objects.create(
            restaurant=self.other_restaurant, category=category, name="Foreign chicken", sku="FOREIGN",
            base_unit=Ingredient.BaseUnit.GRAM, pack_size=1000, current_stock=1000,
        )

    def test_recipe_ingredient_foreign_key_is_enforced_by_database(self):
        row = RecipeIngredient.objects.filter(recipe__menu_item=self.item_a).first()
        self.assertEqual(row.ingredient.pk, row.ingredient_id)
        with self.assertRaises(IntegrityError), transaction.atomic():
            RecipeIngredient.objects.create(
                recipe=row.recipe, ingredient_id=999999999, quantity=1
            )
            connection.check_constraints(table_names=[RecipeIngredient._meta.db_table])

    def test_cross_restaurant_recipe_ingredient_is_rejected_by_reservation(self):
        foreign = self.foreign_ingredient()
        # bulk_create models legacy invalid application data without bypassing FKs.
        RecipeIngredient.objects.bulk_create([
            RecipeIngredient(recipe=self.item_a.recipe, ingredient=foreign, quantity=1)
        ])
        order = self.create_order(reserve=False)
        with self.assertRaises(ValidationError):
            reserve_stock_for_order(order)
        self.assertFalse(order.stock_reservations.exists())
        foreign.refresh_from_db()
        self.assertEqual(foreign.current_stock, Decimal("1000"))

    def test_cross_restaurant_set_component_is_rejected_even_for_legacy_rows(self):
        category = Category.objects.create(restaurant=self.other_restaurant, name="Other food")
        foreign_item = MenuItem.objects.create(category=category, name="Foreign item", price=10)
        SetMenuComponent.objects.bulk_create([
            SetMenuComponent(set_menu=self.set_menu, component=foreign_item, quantity=1)
        ])
        with self.assertRaises(ValidationError):
            get_menu_item_requirements(self.set_menu)

    def test_recipe_builder_saves_real_ingredient_ids_without_duplicating_stock(self):
        self.client.force_login(self.owner)
        ingredient_count = Ingredient.objects.count()
        response = self.client.post(reverse("recipe_builder", args=[self.item_a.pk]), {
            "ingredient_id": [str(self.chicken.pk), str(self.salt.pk)],
            "quantity": ["125", "3"],
            "yield_quantity": "1",
            "instructions": "Cook",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Ingredient.objects.count(), ingredient_count)
        recipe = Recipe.objects.get(menu_item=self.item_a)
        self.assertEqual(dict(recipe.recipe_ingredients.values_list("ingredient_id", "quantity")), {
            self.chicken.pk: Decimal("125"), self.salt.pk: Decimal("3")
        })

    def test_recipe_builder_rejects_other_tenant_ingredient_and_direct_set_recipe(self):
        self.client.force_login(self.owner)
        foreign = self.foreign_ingredient()
        existing_rows = list(self.item_a.recipe.recipe_ingredients.values_list("pk", "ingredient_id", "quantity"))
        response = self.client.post(reverse("recipe_builder", args=[self.item_a.pk]), {
            "ingredient_id": [str(foreign.pk)], "quantity": ["10"], "yield_quantity": "1",
        })
        self.assertIn(response.status_code, [200, 400])
        self.assertEqual(list(self.item_a.recipe.recipe_ingredients.values_list("pk", "ingredient_id", "quantity")), existing_rows)
        response = self.client.post(reverse("recipe_builder", args=[self.set_menu.pk]), {
            "ingredient_id": [str(self.chicken.pk)], "quantity": ["10"], "yield_quantity": "1",
        })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Recipe.objects.filter(menu_item=self.set_menu).exists())


class ReservationTests(OrderFlowFixture, TestCase):
    def test_repeated_reservation_and_sync_reuse_unique_rows(self):
        order = self.create_order()
        original_ids = dict(order.stock_reservations.values_list("ingredient_id", "pk"))
        reserve_stock_for_order(order)
        self.assert_inventory(order)
        order.items.filter(menu_item=self.item_a).update(quantity=3)
        reserve_stock_for_order(order)
        self.assertEqual(dict(order.stock_reservations.values_list("ingredient_id", "pk")), original_ids)
        self.assert_inventory(order, chicken="350", salt="7")

    def test_release_and_rereserve_reuse_rows_without_changing_physical_stock(self):
        order = self.create_order()
        original_ids = dict(order.stock_reservations.values_list("ingredient_id", "pk"))
        release_order_reservations(order)
        release_order_reservations(order)
        self.assertEqual(order.stock_reservations.filter(status="RELEASED").count(), 2)
        self.chicken.refresh_from_db()
        self.assertEqual(self.chicken.current_stock, Decimal("1000"))
        self.assertEqual(self.chicken.available_stock, Decimal("1000"))
        reserve_stock_for_order(order)
        self.assertEqual(dict(order.stock_reservations.values_list("ingredient_id", "pk")), original_ids)
        self.assert_inventory(order)

    def test_removed_ingredient_reservation_is_retained_as_released_then_reused(self):
        order = self.create_order(items=[(self.item_a, 1)])
        salt_reservation = order.stock_reservations.get(ingredient=self.salt)
        # Recipe edits change future explicit sync requirements, never history rows.
        salt_recipe_row = self.item_a.recipe.recipe_ingredients.get(ingredient=self.salt)
        salt_recipe_row.delete()
        reserve_stock_for_order(order)
        salt_reservation.refresh_from_db()
        self.assertEqual(salt_reservation.status, "RELEASED")
        RecipeIngredient.objects.create(recipe=self.item_a.recipe, ingredient=self.salt, quantity=4)
        reserve_stock_for_order(order)
        self.assertEqual(order.stock_reservations.get(ingredient=self.salt).pk, salt_reservation.pk)
        self.assert_inventory(order, chicken="100", salt="2")

    def test_failed_reservation_sync_preserves_previous_reservation_quantities(self):
        order = self.create_order()
        before = list(order.stock_reservations.values_list("pk", "quantity", "status"))
        order.items.filter(menu_item=self.item_a).update(quantity=20)
        with self.assertRaises(ValidationError):
            reserve_stock_for_order(order)
        self.assertEqual(list(order.stock_reservations.values_list("pk", "quantity", "status")), before)
        self.assert_inventory(order)

    def test_consumed_order_can_never_be_reserved_again_even_with_stale_status(self):
        order = self.create_order()
        stale_order = Order.objects.get(pk=order.pk)
        transition_order_status(order, "ACCEPTED")
        transition_order_status(order, "PREPARING")
        for instance in [order, stale_order]:
            with self.assertRaises(ValidationError):
                reserve_stock_for_order(instance)
        self.assert_inventory(order, consumed=True)

    def test_advanced_order_cannot_release_active_reservations_as_if_uncooked(self):
        order = self.create_order()
        Order.objects.filter(pk=order.pk).update(status="PREPARING")
        with self.assertRaises(ValidationError):
            release_order_reservations(order)
        self.assert_inventory(order)

    def test_preparing_consumes_original_reservation_after_recipe_change(self):
        order = self.create_order()
        RecipeIngredient.objects.filter(recipe__menu_item=self.item_a, ingredient=self.chicken).update(quantity=800)
        transition_order_status(order, "ACCEPTED")
        transition_order_status(order, "PREPARING")
        self.assert_inventory(order, consumed=True)


class ConsumptionAndReconciliationTests(OrderFlowFixture, TestCase):
    def historical_order(self, status="COMPLETED"):
        order = self.create_order()
        Order.objects.filter(pk=order.pk).update(status=status)
        order.refresh_from_db()
        return order

    def record_consumption(self, order, ingredient, quantity):
        return StockTransaction.objects.create(
            order=order, ingredient=ingredient,
            transaction_type=StockTransaction.TransactionType.CONSUMPTION,
            quantity=quantity, note="Historical consumption",
        )

    def test_consumption_requires_accepted_or_later_status(self):
        order = self.create_order()
        with self.assertRaises(ValidationError):
            consume_order_reservations(order)
        self.assert_inventory(order)

    def test_matching_existing_ledger_finalizes_reservation_without_deducting_again(self):
        order = self.historical_order()
        self.record_consumption(order, self.chicken, 250)
        self.record_consumption(order, self.salt, 5)
        Ingredient.objects.filter(pk=self.chicken.pk).update(current_stock=750)
        Ingredient.objects.filter(pk=self.salt.pk).update(current_stock=95)
        consume_order_reservations(order)
        consume_order_reservations(order)
        self.assert_inventory(order, consumed=True)

    def test_existing_one_ingredient_ledger_only_deducts_missing_ingredient(self):
        order = self.historical_order()
        self.record_consumption(order, self.chicken, 250)
        Ingredient.objects.filter(pk=self.chicken.pk).update(current_stock=750)
        consume_order_reservations(order)
        self.assert_inventory(order, consumed=True)

    def test_partial_quantity_ledger_rejects_entire_consumption_without_changes(self):
        order = self.historical_order()
        self.record_consumption(order, self.chicken, 100)
        with self.assertRaises(ValidationError):
            consume_order_reservations(order)
        self.assertEqual(order.stock_transactions.count(), 1)
        self.assertEqual(order.stock_reservations.filter(status="ACTIVE").count(), 2)
        self.chicken.refresh_from_db()
        self.salt.refresh_from_db()
        self.assertEqual(self.chicken.current_stock, Decimal("1000"))
        self.assertEqual(self.salt.current_stock, Decimal("100"))

    def test_duplicate_ledger_entries_are_flagged_even_when_sum_matches(self):
        order = self.historical_order()
        self.record_consumption(order, self.chicken, 100)
        self.record_consumption(order, self.chicken, 150)
        with self.assertRaises(ValidationError):
            consume_order_reservations(order)
        self.assertEqual(order.stock_transactions.count(), 2)
        self.assertEqual(order.stock_reservations.filter(status="ACTIVE").count(), 2)

    def test_consumed_reservation_missing_ledger_is_not_deducted_again(self):
        order = self.historical_order()
        order.stock_reservations.filter(ingredient=self.chicken).update(status="CONSUMED")
        with self.assertRaises(ValidationError):
            consume_order_reservations(order)
        self.assertFalse(order.stock_transactions.exists())
        self.chicken.refresh_from_db()
        self.assertEqual(self.chicken.current_stock, Decimal("1000"))

    def test_ledger_without_reservation_is_flagged(self):
        order = self.create_order(reserve=False)
        Order.objects.filter(pk=order.pk).update(status="COMPLETED")
        self.record_consumption(order, self.chicken, 250)
        with self.assertRaises(ValidationError):
            consume_order_reservations(order)
        self.assertEqual(order.stock_transactions.count(), 1)
        self.assertFalse(order.stock_reservations.exists())

    def test_released_reservation_with_consumption_ledger_is_flagged(self):
        order = self.historical_order()
        order.stock_reservations.filter(ingredient=self.chicken).update(status="RELEASED")
        self.record_consumption(order, self.chicken, 250)
        with self.assertRaises(ValidationError):
            consume_order_reservations(order)
        self.assertEqual(order.stock_transactions.count(), 1)
        self.assertEqual(order.stock_reservations.get(ingredient=self.chicken).status, "RELEASED")

    def test_reconciliation_is_dry_run_by_default_and_apply_is_repeatable(self):
        order = self.historical_order()
        order_item_ids = list(order.items.values_list("pk", flat=True))
        reservation_ids = list(order.stock_reservations.values_list("pk", flat=True))
        output = StringIO()
        call_command("reconcile_order_inventory", "--order-id", str(order.pk), stdout=output)
        self.assertIn(str(order.pk), output.getvalue())
        self.assert_inventory(order)
        for _ in range(2):
            call_command("reconcile_order_inventory", "--apply", "--order-id", str(order.pk), stdout=StringIO())
            self.assert_inventory(order, consumed=True)
        order.refresh_from_db()
        self.assertEqual(order.status, "COMPLETED")
        self.assertEqual(list(order.items.values_list("pk", flat=True)), order_item_ids)
        self.assertEqual(list(order.stock_reservations.values_list("pk", flat=True)), reservation_ids)

    def test_reconciliation_leaves_new_orders_active(self):
        order = self.create_order()
        call_command("reconcile_order_inventory", "--apply", stdout=StringIO())
        self.assert_inventory(order)
        order.refresh_from_db()
        self.assertEqual(order.status, "NEW")

    def test_reconciliation_apply_repairs_valid_orders_and_skips_ambiguous_order_atomically(self):
        valid = self.historical_order()
        invalid = self.historical_order()
        self.record_consumption(invalid, self.chicken, 50)
        with self.assertRaises(CommandError):
            call_command("reconcile_order_inventory", "--apply", stdout=StringIO(), stderr=StringIO())
        self.chicken.refresh_from_db()
        self.salt.refresh_from_db()
        self.assertEqual(self.chicken.current_stock, Decimal("750"))
        self.assertEqual(self.salt.current_stock, Decimal("95"))
        self.assertEqual(valid.stock_reservations.filter(status="CONSUMED").count(), 2)
        self.assertEqual(valid.stock_transactions.count(), 2)
        self.assertEqual(invalid.stock_reservations.filter(status="ACTIVE").count(), 2)
        self.assertEqual(invalid.stock_transactions.count(), 1)

    def test_reconciliation_dry_run_detects_mismatch_and_does_not_repair_it(self):
        order = self.historical_order()
        self.record_consumption(order, self.chicken, 50)
        output = StringIO()
        with self.assertRaises(CommandError):
            call_command("reconcile_order_inventory", "--order-id", str(order.pk), stdout=output, stderr=output)
        self.assertIn(str(order.pk), output.getvalue())
        self.assertEqual(order.stock_transactions.count(), 1)
        self.assertEqual(order.stock_reservations.filter(status="ACTIVE").count(), 2)
        self.chicken.refresh_from_db()
        self.assertEqual(self.chicken.current_stock, Decimal("1000"))

    def test_advanced_transition_repairs_legacy_inventory_before_advancing(self):
        order = self.historical_order(status="READY")
        order.payment_status = "PAID"
        order.save(update_fields=["payment_status"])
        transition_order_status(order, "SERVED")
        self.assert_inventory(order, consumed=True)
        transition_order_status(order, "COMPLETED")
        self.assert_inventory(order, consumed=True)

    def test_advanced_transition_with_no_reservations_is_blocked(self):
        order = self.create_order(status="READY", reserve=False)
        with self.assertRaises(ValidationError):
            transition_order_status(order, "SERVED")
        order.refresh_from_db()
        self.assertEqual(order.status, "READY")
        self.assertFalse(order.stock_transactions.exists())

    def test_reconciliation_physical_shortage_is_atomic(self):
        order = self.historical_order()
        Ingredient.objects.filter(pk=self.salt.pk).update(current_stock=1)
        with self.assertRaises(ValidationError):
            consume_order_reservations(order)
        self.chicken.refresh_from_db()
        self.assertEqual(self.chicken.current_stock, Decimal("1000"))
        self.assertFalse(order.stock_transactions.exists())
        self.assertEqual(order.stock_reservations.filter(status="ACTIVE").count(), 2)
