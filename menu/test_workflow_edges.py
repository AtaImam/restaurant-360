"""Database regressions for order inputs, tracking and recipe/inventory edits."""

import json
import re
import shutil
import subprocess
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from inventory.models import Ingredient, IngredientCategory, Recipe, RecipeIngredient, StockReservation, StockTransaction
from orders.models import Order, OrderItem
from orders.services import transition_order_status
from orders.test_flow import OrderFlowFixture
from users.models import User


class CustomerJourneyTests(OrderFlowFixture, TestCase):
    def test_menu_cart_checkout_and_refresh_use_real_routes(self):
        args = [self.restaurant.pk, self.table.pk]
        menu_url = reverse("customer_menu", args=args)
        self.assertContains(self.client.get(menu_url), self.item_a.name)
        self.assertContains(
            self.client.get(reverse("item_detail", args=[*args, self.set_menu.pk])),
            self.set_menu.name,
        )
        add_url = reverse("add_to_cart", args=[*args, self.item_a.pk])
        self.assertEqual(self.client.get(add_url).status_code, 405)
        self.assertRedirects(self.client.post(add_url), menu_url)
        cart_url = reverse("cart", args=args)
        cart_page = self.client.get(cart_url)
        self.assertEqual(cart_page.context["total"], Decimal("100"))
        self.assertRedirects(
            self.client.get(reverse("increase_cart_item", args=[*args, self.item_a.pk])), cart_url,
        )
        self.assertEqual(self.client.get(cart_url).context["total"], Decimal("200"))
        self.assertRedirects(
            self.client.get(reverse("decrease_cart_item", args=[*args, self.item_a.pk])), cart_url,
        )
        checkout_url = reverse("checkout", args=args)
        self.assertEqual(self.client.get(checkout_url).context["total"], Decimal("100"))
        self.assertEqual(self.qr_checkout().status_code, 302)
        order = Order.objects.get()
        self.assert_inventory(order, chicken="100", salt="2")
        for _ in range(2):
            self.assertEqual(self.client.get(reverse("order_success", args=[order.pk])).status_code, 200)
            self.assertEqual(self.client.get(reverse("order_status_api", args=[order.pk])).json()["status"], "NEW")
        self.assertEqual(self.qr_checkout().status_code, 302)
        self.assertEqual(Order.objects.count(), 1)
        self.assert_inventory(order, chicken="100", salt="2")

    def test_decrease_and_remove_never_leave_zero_quantity_lines(self):
        args = [self.restaurant.pk, self.table.pk, self.item_a.pk]
        self.client.post(reverse("add_to_cart", args=args))
        self.client.get(reverse("decrease_cart_item", args=args))
        self.assertFalse(self.client.get(reverse("cart", args=args[:2])).context["cart"])
        self.client.post(reverse("add_to_cart", args=args))
        self.client.get(reverse("remove_cart_item", args=args))
        self.assertFalse(self.client.get(reverse("cart", args=args[:2])).context["cart"])

    def test_customer_staff_templates_and_rendered_javascript_are_valid(self):
        order = self.create_order()
        self.client.force_login(self.owner)
        urls = [
            reverse("customer_menu", args=[self.restaurant.pk, self.table.pk]),
            reverse("item_detail", args=[self.restaurant.pk, self.table.pk, self.item_a.pk]),
            reverse("item_detail", args=[self.restaurant.pk, self.table.pk, self.set_menu.pk]),
            reverse("order_success", args=[order.pk]),
            reverse("pos_dashboard"), reverse("owner_dashboard"), reverse("orders_list"),
            reverse("order_detail", args=[order.pk]), reverse("kitchen_dashboard"),
            reverse("recipe_builder", args=[self.item_a.pk]),
            reverse("recipe_builder", args=[self.set_menu.pk]),
        ]
        scripts = []
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                for attributes, script in re.findall(r"<script\b([^>]*)>(.*?)</script>", response.content.decode(), re.S):
                    if "application/json" not in attributes and script.strip():
                        scripts.append({"url": url, "script": script})
        if shutil.which("node"):
            result = subprocess.run(
                [shutil.which("node"), "-e",
                 'const vm=require("node:vm"); for(const item of JSON.parse(require("node:fs").readFileSync(0,"utf8"))) new vm.Script(item.script,{filename:item.url});'],
                input=json.dumps(scripts), text=True, capture_output=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        waiter = User.objects.create_user(username="page-waiter", role="waiter", restaurant=self.restaurant)
        self.client.force_login(waiter)
        for url in [reverse("waiter:dashboard"), reverse("orders_list"), reverse("order_detail", args=[order.pk])]:
            self.assertEqual(self.client.get(url).status_code, 200)


class WorkflowInputTests(OrderFlowFixture, TestCase):
    def test_pos_rejects_bad_quantities_without_accepting_other_lines(self):
        self.client.force_login(self.owner)
        for quantity in (0, -1, 1.5, True, None, "abc"):
            with self.subTest(quantity=quantity):
                response = self.pos_checkout(items=[(self.item_a, 1), (self.item_b, quantity)])
                self.assertEqual(response.status_code, 400)
                self.assertFalse(Order.objects.exists())
                self.assertFalse(OrderItem.objects.exists())
                self.assertFalse(StockReservation.objects.exists())

    def test_pos_rejects_nonfinite_billing_and_nonobject_json(self):
        self.client.force_login(self.owner)
        for field in ("discount_amount", "service_percent", "vat_percent"):
            for value in ("NaN", "Infinity", "-Infinity"):
                with self.subTest(field=field, value=value):
                    self.assertEqual(self.pos_checkout(**{field: value}).status_code, 400)
        for data in ([], None, "not an object"):
            response = self.client.post(
                reverse("create_pos_order"), json.dumps(data), content_type="application/json",
            )
            self.assertEqual(response.status_code, 400)
        self.assertFalse(Order.objects.exists())

    def test_qr_rejects_invalid_line_preserving_entire_cart(self):
        key, cart = self.seed_cart(items=[(self.item_a, 1), (self.item_b, 0)])
        response = self.qr_checkout()
        self.assertContains(response, "positive whole number", status_code=400)
        self.assertEqual(self.client.session[key], cart)
        self.assertFalse(Order.objects.exists())
        self.assertFalse(StockReservation.objects.exists())

    def test_overdue_tracking_does_not_claim_or_advance_readiness(self):
        order = self.create_order()
        transition_order_status(order, "ACCEPTED")
        transition_order_status(order, "PREPARING")
        Order.objects.filter(pk=order.pk).update(created_at=timezone.now() - timedelta(hours=3))
        response = self.client.get(reverse("order_status_api", args=[order.pk]))
        payload = response.json()
        self.assertEqual(payload["status"], "PREPARING")
        self.assertIsNone(payload["remaining_minutes"])
        self.assertIn("taking longer than estimated", payload["message"])
        page = self.client.get(reverse("order_success", args=[order.pk]))
        self.assertContains(page, payload["title"])
        self.assertContains(page, payload["message"])
        order.refresh_from_db()
        self.assertEqual(order.status, "PREPARING")
        self.assertEqual(order.stock_transactions.count(), 2)


class RecipeBuilderTests(OrderFlowFixture, TestCase):
    def setUp(self):
        self.client.force_login(self.owner)

    def test_recipe_edit_preserves_yield_existing_fk_row_and_visibility(self):
        recipe = self.item_a.recipe
        row = recipe.recipe_ingredients.get(ingredient=self.chicken)
        row.is_customer_visible = True
        row.save(update_fields=["is_customer_visible"])
        response = self.client.post(reverse("recipe_builder", args=[self.item_a.pk]), {
            "ingredient_id": [self.chicken.pk, self.salt.pk],
            "quantity": ["220", "5"],
            "instructions": "Updated instructions",
        })
        self.assertEqual(response.status_code, 302)
        recipe.refresh_from_db()
        row.refresh_from_db()
        self.assertEqual(recipe.yield_quantity, Decimal("2"))
        self.assertEqual(row.quantity, Decimal("220"))
        self.assertTrue(row.is_customer_visible)
        self.assertEqual(Ingredient.objects.count(), 2)

    def test_set_menu_shows_components_and_rejects_direct_recipe(self):
        url = reverse("recipe_builder", args=[self.set_menu.pk])
        self.assertContains(self.client.get(url), "recipes of these component foods")
        response = self.client.post(url, {
            "ingredient_id": [self.chicken.pk], "quantity": ["100"],
        })
        self.assertContains(response, "direct ingredient recipes are not allowed", status_code=400)
        self.assertFalse(Recipe.objects.filter(menu_item=self.set_menu).exists())
        self.assertEqual(self.set_menu.set_components.count(), 2)

    def test_builder_rejects_foreign_ingredient_and_incomplete_rows_atomically(self):
        foreign_category = IngredientCategory.objects.create(restaurant=self.other_restaurant, name="Other")
        foreign = Ingredient.objects.create(
            restaurant=self.other_restaurant, category=foreign_category, name="Other ingredient",
            sku="FOREIGN", base_unit="G", pack_size=100, current_stock=100,
        )
        original = list(self.item_a.recipe.recipe_ingredients.values_list("pk", "ingredient_id", "quantity"))
        for ids, quantities in (
            ([self.chicken.pk, foreign.pk], ["100", "10"]),
            ([self.chicken.pk, self.salt.pk], ["100"]),
            ([self.chicken.pk], ["NaN"]),
        ):
            response = self.client.post(reverse("recipe_builder", args=[self.item_a.pk]), {
                "ingredient_id": ids, "quantity": quantities,
            })
            self.assertEqual(response.status_code, 400)
            self.assertEqual(
                list(self.item_a.recipe.recipe_ingredients.values_list("pk", "ingredient_id", "quantity")),
                original,
            )


class IngredientEditTests(OrderFlowFixture, TestCase):
    def setUp(self):
        self.client.force_login(self.owner)

    def edit_data(self, **overrides):
        return {
            "restaurant": self.restaurant.pk, "category": self.ingredient_category.pk,
            "name": "Updated chicken", "sku": "CHICKEN", "base_unit": "G",
            "pack_size": "1000", "current_pack_price": "0", "current_stock": "1000",
            "original_current_stock": "1000", "minimum_level": "0", "target_level": "0",
            "effective_date": "2026-09-11", "is_active": "on", **overrides,
        }

    def post_edit(self, **overrides):
        return self.client.post(reverse("ingredient_edit", args=[self.chicken.pk]), self.edit_data(**overrides))

    def preparing_order(self):
        order = self.create_order()
        transition_order_status(order, "ACCEPTED")
        transition_order_status(order, "PREPARING")
        return order

    def test_stale_metadata_edit_preserves_consumed_physical_stock(self):
        order = self.preparing_order()
        response = self.post_edit()
        self.assertEqual(response.status_code, 302)
        self.chicken.refresh_from_db()
        self.assertEqual(self.chicken.current_stock, Decimal("750"))
        self.assertEqual(self.chicken.name, "Updated chicken")
        self.assertEqual(self.chicken.stock_transactions.count(), 1)
        self.assertEqual(order.stock_transactions.count(), 2)

    def test_stale_explicit_stock_adjustment_rejected_atomically(self):
        self.preparing_order()
        response = self.post_edit(current_stock="900")
        self.assertContains(response, "Stock changed since this form was loaded", status_code=400)
        self.chicken.refresh_from_db()
        self.assertEqual(self.chicken.current_stock, Decimal("750"))
        self.assertEqual(self.chicken.name, "Chicken")
        self.assertEqual(self.chicken.stock_transactions.count(), 1)

    def test_stock_cannot_drop_below_active_reservations(self):
        order = self.create_order()
        response = self.post_edit(current_stock="200")
        self.assertContains(response, "active order reservations", status_code=400)
        self.chicken.refresh_from_db()
        self.assertEqual(self.chicken.current_stock, Decimal("1000"))
        self.assertEqual(self.chicken.reserved_stock, Decimal("250"))
        self.assertFalse(order.stock_transactions.exists())

    def test_explicit_stock_adjustment_has_matching_ledger(self):
        response = self.post_edit(current_stock="900")
        self.assertEqual(response.status_code, 302)
        self.chicken.refresh_from_db()
        self.assertEqual(self.chicken.current_stock, Decimal("900"))
        ledger = self.chicken.stock_transactions.get()
        self.assertEqual(ledger.transaction_type, StockTransaction.TransactionType.ADJUSTMENT_OUT)
        self.assertEqual(ledger.quantity, Decimal("100"))

    def test_used_ingredient_cannot_change_unit_or_restaurant(self):
        other_group = IngredientCategory.objects.create(restaurant=self.other_restaurant, name="Other")
        for overrides in (
            {"base_unit": "ML"},
            {"restaurant": self.other_restaurant.pk, "category": other_group.pk},
        ):
            response = self.post_edit(**overrides)
            self.assertContains(response, "Restaurant and base unit cannot change", status_code=400)
            self.chicken.refresh_from_db()
            self.assertEqual(self.chicken.restaurant_id, self.restaurant.pk)
            self.assertEqual(self.chicken.base_unit, "G")

    def test_invalid_price_history_rolls_back_stock_and_metadata(self):
        response = self.post_edit(current_stock="900", current_pack_price="20", effective_date="bad-date")
        self.assertEqual(response.status_code, 400)
        self.chicken.refresh_from_db()
        self.assertEqual(self.chicken.current_stock, Decimal("1000"))
        self.assertEqual(self.chicken.name, "Chicken")
        self.assertFalse(self.chicken.stock_transactions.exists())
        self.assertFalse(self.chicken.price_history.exists())
