"""Database-backed regression coverage for the QR/POS-to-kitchen workflow."""

import json
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from inventory.models import (
    Ingredient,
    IngredientCategory,
    Recipe,
    RecipeIngredient,
    StockReservation,
    StockTransaction,
)
from inventory.services import reserve_stock_for_order
from menu.models import Category, MenuItem, SetMenuComponent
from menu.views import get_cart_key
from orders.models import Order, OrderItem
from orders.services import transition_order_status
from restaurant.models import Restaurant, Table
from users.models import User


class OrderFlowFixture:
    """Real related records; supplied QR path prevents fixture file writes."""

    @classmethod
    def setUpTestData(cls):
        cls.restaurant = Restaurant.objects.create(name="Flow restaurant", address="Dhaka")
        cls.other_restaurant = Restaurant.objects.create(name="Other restaurant", address="Dhaka")
        cls.table = Table.objects.create(
            restaurant=cls.restaurant, table_number=1, qr_code="test-fixtures/table.png"
        )
        cls.category = Category.objects.create(restaurant=cls.restaurant, name="Mains")
        cls.ingredient_category = IngredientCategory.objects.create(
            restaurant=cls.restaurant, name="Kitchen"
        )
        cls.chicken = Ingredient.objects.create(
            restaurant=cls.restaurant,
            category=cls.ingredient_category,
            name="Chicken",
            sku="CHICKEN",
            base_unit=Ingredient.BaseUnit.GRAM,
            pack_size=1000,
            current_stock=1000,
        )
        cls.salt = Ingredient.objects.create(
            restaurant=cls.restaurant,
            category=cls.ingredient_category,
            name="Salt",
            sku="SALT",
            base_unit=Ingredient.BaseUnit.GRAM,
            pack_size=100,
            current_stock=100,
        )
        cls.item_a = MenuItem.objects.create(category=cls.category, name="Chicken A", price=100)
        cls.item_b = MenuItem.objects.create(category=cls.category, name="Chicken B", price=80)
        recipe_a = Recipe.objects.create(menu_item=cls.item_a, yield_quantity=2)
        recipe_b = Recipe.objects.create(menu_item=cls.item_b, yield_quantity=1)
        RecipeIngredient.objects.bulk_create([
            RecipeIngredient(recipe=recipe_a, ingredient=cls.chicken, quantity=200),
            RecipeIngredient(recipe=recipe_a, ingredient=cls.salt, quantity=4),
            RecipeIngredient(recipe=recipe_b, ingredient=cls.chicken, quantity=50),
            RecipeIngredient(recipe=recipe_b, ingredient=cls.salt, quantity=1),
        ])
        cls.set_menu = MenuItem.objects.create(
            category=cls.category, name="Family Set", price=250,
            item_type=MenuItem.ItemType.SET_MENU,
        )
        SetMenuComponent.objects.create(set_menu=cls.set_menu, component=cls.item_a, quantity="1.5")
        SetMenuComponent.objects.create(set_menu=cls.set_menu, component=cls.item_b, quantity=2)
        cls.owner = User.objects.create_user(
            username="flow-owner", role="owner", restaurant=cls.restaurant
        )

    def create_order(self, *, items=None, status="NEW", reserve=True):
        items = items if items is not None else [(self.item_a, 2), (self.item_b, 1)]
        total = sum((item.price * quantity for item, quantity in items), Decimal("0"))
        order = Order.objects.create(
            restaurant=self.restaurant, table=self.table, order_type="DINE_IN",
            status=status, subtotal=total, total_amount=total,
        )
        OrderItem.objects.bulk_create([
            OrderItem(order=order, menu_item=item, quantity=quantity, price=item.price)
            for item, quantity in items
        ])
        if reserve:
            reserve_stock_for_order(order)
        return order

    def assert_inventory(self, order, *, consumed=False, chicken="250", salt="5"):
        """Check persisted quantities, reservation states, availability and ledger."""
        quantities = {self.chicken.pk: Decimal(chicken), self.salt.pk: Decimal(salt)}
        starting = {self.chicken.pk: Decimal("1000"), self.salt.pk: Decimal("100")}
        rows = list(order.stock_reservations.order_by("ingredient_id"))
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.ingredient_id: row.quantity for row in rows}, quantities)
        expected_status = StockReservation.Status.CONSUMED if consumed else StockReservation.Status.ACTIVE
        self.assertEqual({row.status for row in rows}, {expected_status})
        for ingredient in Ingredient.objects.filter(pk__in=quantities):
            expected_stock = starting[ingredient.pk] - (quantities[ingredient.pk] if consumed else 0)
            self.assertEqual(ingredient.current_stock, expected_stock)
            self.assertEqual(ingredient.reserved_stock, 0 if consumed else quantities[ingredient.pk])
            self.assertEqual(ingredient.available_stock, starting[ingredient.pk] - quantities[ingredient.pk])
        ledger = list(order.stock_transactions.filter(
            transaction_type=StockTransaction.TransactionType.CONSUMPTION
        ))
        self.assertEqual(len(ledger), 2 if consumed else 0)
        if consumed:
            self.assertEqual({row.ingredient_id: row.quantity for row in ledger}, quantities)

    def seed_cart(self, items=None):
        items = items if items is not None else [(self.item_a, 2), (self.item_b, 1)]
        cart = {
            str(item.pk): {"name": item.name, "price": "0.01", "quantity": quantity}
            for item, quantity in items
        }
        session = self.client.session
        key = get_cart_key(self.restaurant.pk, self.table.pk)
        session[key] = cart
        session.save()
        return key, cart

    def qr_checkout(self):
        return self.client.post(reverse("checkout", args=[self.restaurant.pk, self.table.pk]), {
            "order_type": "DINE_IN"
        })

    def pos_checkout(self, items=None, **extra):
        items = items if items is not None else [(self.item_a, 2), (self.item_b, 1)]
        data = {
            "order_type": "TAKEAWAY",
            "items": [{"id": item.pk, "quantity": quantity, "price": "0.01"} for item, quantity in items],
            **extra,
        }
        return self.client.post(reverse("create_pos_order"), json.dumps(data), content_type="application/json")


class OrderLifecycleTests(OrderFlowFixture, TestCase):
    def test_qr_order_entire_staff_flow_and_live_tracking(self):
        key, _ = self.seed_cart()
        response = self.qr_checkout()
        self.assertEqual(response.status_code, 302)
        order = Order.objects.get()
        self.assertEqual(response.url, reverse("order_success", args=[order.pk]))
        self.assertEqual(order.total_amount, Decimal("280.00"))
        self.assertEqual(self.client.session[key], {})
        self.assertEqual(order.status, "NEW")
        self.assert_inventory(order)

        stages = [
            ("NEW", None),
            ("ACCEPTED", "update_order_status"),
            ("PREPARING", "update_order_status"),
            ("READY", "update_order_status"),
            ("SERVED", "order_detail"),
            ("COMPLETED", "order_detail"),
        ]
        for stage_index, (status, endpoint) in enumerate(stages):
            with self.subTest(status=status):
                if endpoint:
                    response = self.client.post(reverse(endpoint, args=[order.pk]), {"status": status})
                    self.assertEqual(response.status_code, 302)
                order.refresh_from_db()
                self.assertEqual(order.status, status)
                self.assert_inventory(order, consumed=stage_index >= 2)
                api = self.client.get(reverse("order_status_api", args=[order.pk]))
                self.assertEqual(api.status_code, 200)
                self.assertIn("no-store", api["Cache-Control"])
                payload = api.json()
                self.assertEqual(payload["status"], status)
                self.assertEqual(payload["stage_index"], stage_index)
                self.assertEqual(payload["is_terminal"], status == "COMPLETED")
                if status in {"ACCEPTED", "PREPARING"}:
                    self.assertIsInstance(payload["remaining_minutes"], int)
                else:
                    self.assertIsNone(payload["remaining_minutes"])
                page = self.client.get(reverse("order_success", args=[order.pk]))
                self.assertEqual(page.context["order"].status, status)
                self.assertContains(page, payload["title"])
                self.assertContains(page, "initialOrderStatus")
                self.assertContains(page, reverse("order_status_api", args=[order.pk]))
                order.refresh_from_db()
                self.assertEqual(order.status, status)

    def test_pos_order_reserves_and_uses_identical_consumption_flow(self):
        self.client.force_login(self.owner)
        response = self.pos_checkout()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        order = Order.objects.get(pk=response.json()["order_id"])
        self.assertEqual(order.order_type, "TAKEAWAY")
        self.assertIsNone(order.table)
        self.assertEqual(order.total_amount, Decimal("280.00"))
        self.assert_inventory(order)
        for index, status in enumerate(["ACCEPTED", "PREPARING", "READY", "SERVED", "COMPLETED"]):
            transition_order_status(order, status)
            self.assert_inventory(order, consumed=index >= 1)

    def test_repeated_preparing_posts_do_not_advance_or_consume_twice(self):
        order = self.create_order()
        transition_order_status(order, "ACCEPTED")
        for _ in range(3):
            response = self.client.post(reverse("update_order_status", args=[order.pk]), {"status": "PREPARING"})
            self.assertEqual(response.status_code, 302)
            order.refresh_from_db()
            self.assertEqual(order.status, "PREPARING")
            self.assert_inventory(order, consumed=True)

    def test_stale_instances_use_locked_database_status_and_repeat_safely(self):
        order = self.create_order()
        stale = Order.objects.get(pk=order.pk)
        transition_order_status(order, "ACCEPTED")
        transition_order_status(stale, "PREPARING")
        transition_order_status(order, "PREPARING")
        self.assert_inventory(order, consumed=True)
        with self.assertRaises(ValidationError):
            transition_order_status(stale, "ACCEPTED")
        self.assert_inventory(order, consumed=True)

    def test_invalid_skips_unknown_status_and_reverse_transitions_are_rejected(self):
        order = self.create_order()
        for invalid in ["PREPARING", "READY", "SERVED", "COMPLETED", "CANCELLED", "unknown", ""]:
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                transition_order_status(order, invalid)
        self.assert_inventory(order)
        transition_order_status(order, "ACCEPTED")
        with self.assertRaises(ValidationError):
            transition_order_status(order, "NEW")
        for status in ["PREPARING", "READY", "SERVED", "COMPLETED"]:
            transition_order_status(order, status)
        with self.assertRaises(ValidationError):
            transition_order_status(order, "READY")
        self.assert_inventory(order, consumed=True)

    def test_shortage_at_preparing_rolls_back_status_reservations_and_all_ingredients(self):
        order = self.create_order()
        transition_order_status(order, "ACCEPTED")
        Ingredient.objects.filter(pk=self.salt.pk).update(current_stock=1)
        with self.assertRaises(ValidationError):
            transition_order_status(order, "PREPARING")
        order.refresh_from_db()
        self.chicken.refresh_from_db()
        self.salt.refresh_from_db()
        self.assertEqual(order.status, "ACCEPTED")
        self.assertEqual(self.chicken.current_stock, Decimal("1000"))
        self.assertEqual(self.salt.current_stock, Decimal("1"))
        self.assertEqual(order.stock_reservations.filter(status="ACTIVE").count(), 2)
        self.assertFalse(order.stock_transactions.exists())

    def test_kitchen_rejects_missing_target_and_cannot_serve_or_complete(self):
        order = self.create_order()
        self.client.force_login(self.owner)
        endpoint = reverse("update_order_status", args=[order.pk])
        self.assertEqual(self.client.get(endpoint).status_code, 405)
        self.client.post(endpoint, {})
        order.refresh_from_db()
        self.assertEqual(order.status, "NEW")
        for status in ["ACCEPTED", "PREPARING", "READY"]:
            transition_order_status(order, status)
        self.client.post(endpoint, {"status": "SERVED"})
        order.refresh_from_db()
        self.assertEqual(order.status, "READY")
        self.assert_inventory(order, consumed=True)

    def test_kitchen_accepts_waiter_role_and_preserves_restaurant_scope(self):
        order = self.create_order()
        waiter = User.objects.create_user(username="flow-waiter", role="waiter", restaurant=self.restaurant)
        self.client.force_login(waiter)
        self.client.post(reverse("update_order_status", args=[order.pk]), {"status": "ACCEPTED"})
        order.refresh_from_db()
        self.assertEqual(order.status, "ACCEPTED")
        self.client.post(reverse("order_detail", args=[order.pk]), {"status": "PREPARING"})
        order.refresh_from_db()
        self.assertEqual(order.status, "ACCEPTED")
        self.assert_inventory(order)
        self.client.post(reverse("update_order_status", args=[order.pk]), {"status": "PREPARING"})
        order.refresh_from_db()
        self.assertEqual(order.status, "PREPARING")
        self.assert_inventory(order, consumed=True)
        other_owner = User.objects.create_user(
            username="other-owner", role="owner", restaurant=self.other_restaurant
        )
        self.client.force_login(other_owner)
        self.assertEqual(self.client.post(reverse("update_order_status", args=[order.pk]), {
            "status": "PREPARING"
        }).status_code, 404)
        self.assertEqual(self.client.get(reverse("order_detail", args=[order.pk])).status_code, 404)

    def test_order_pages_and_pos_work_without_login(self):
        for endpoint in ["kitchen_dashboard", "orders_list", "pos_dashboard"]:
            self.assertEqual(self.client.get(reverse(endpoint)).status_code, 200)
        response = self.pos_checkout()
        self.assertEqual(response.status_code, 200)
        order = Order.objects.get(pk=response.json()["order_id"])
        self.assertContains(self.client.get(reverse("kitchen_dashboard")), f"Order #{order.pk}")
        self.assertEqual(self.client.get(reverse("order_detail", args=[order.pk])).status_code, 200)
        self.assertEqual(order.status, "NEW")
        self.assertEqual(Order.objects.count(), 1)
        self.assert_inventory(order)


class CheckoutFailureTests(OrderFlowFixture, TestCase):
    def assert_nothing_created(self):
        self.assertFalse(Order.objects.exists())
        self.assertFalse(OrderItem.objects.exists())
        self.assertFalse(StockReservation.objects.exists())
        self.assertFalse(StockTransaction.objects.exists())
        self.chicken.refresh_from_db()
        self.assertEqual(self.chicken.current_stock, Decimal("1000"))

    def test_qr_insufficient_stock_rolls_back_and_keeps_cart_with_visible_error(self):
        key, cart = self.seed_cart()
        Ingredient.objects.filter(pk=self.salt.pk).update(current_stock=1)
        response = self.qr_checkout()
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.context["stock_error"])
        self.assertContains(response, "Salt", status_code=409)
        self.assertEqual(self.client.session[key], cart)
        self.assert_nothing_created()

    def test_pos_insufficient_stock_rolls_back_order_items_and_reservations(self):
        self.client.force_login(self.owner)
        Ingredient.objects.filter(pk=self.salt.pk).update(current_stock=1)
        response = self.pos_checkout()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error_type"], "STOCK_UNAVAILABLE")
        self.assertIn("Salt", response.json()["message"])
        self.assert_nothing_created()

    def test_other_order_reservations_reduce_available_stock_at_both_checkouts(self):
        existing = self.create_order(items=[(self.item_a, 9)])
        self.seed_cart()
        self.assertEqual(self.qr_checkout().status_code, 409)
        self.client.force_login(self.owner)
        self.assertEqual(self.pos_checkout().status_code, 409)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(OrderItem.objects.count(), 1)
        self.assert_inventory(existing, chicken="900", salt="18")

    def test_qr_set_menu_uses_component_recipes(self):
        self.seed_cart([(self.set_menu, 2)])
        self.assertEqual(self.qr_checkout().status_code, 302)
        order = Order.objects.get()
        self.assert_inventory(order, chicken="500", salt="10")
        transition_order_status(order, "ACCEPTED")
        transition_order_status(order, "PREPARING")
        self.assert_inventory(order, consumed=True, chicken="500", salt="10")

    def test_malformed_qr_quantity_does_not_silently_order_remaining_items(self):
        key, cart = self.seed_cart()
        cart[str(self.item_a.pk)]["quantity"] = "invalid"
        session = self.client.session
        session[key] = cart
        session.save()
        self.assertEqual(self.qr_checkout().status_code, 400)
        self.assertEqual(self.client.session[key], cart)
        self.assert_nothing_created()

    def test_pos_does_not_accept_foreign_restaurant_items(self):
        other_category = Category.objects.create(restaurant=self.other_restaurant, name="Other mains")
        other_item = MenuItem.objects.create(category=other_category, name="Other food", price=20)
        self.client.force_login(self.owner)
        response = self.pos_checkout([(other_item, 1)])
        self.assertEqual(response.status_code, 400)
        self.assert_nothing_created()

    def test_pos_duplicate_cart_rows_cannot_bypass_combined_stock_requirement(self):
        self.client.force_login(self.owner)
        response = self.pos_checkout([(self.item_a, 6), (self.item_a, 6)])
        self.assertEqual(response.status_code, 409)
        self.assert_nothing_created()
