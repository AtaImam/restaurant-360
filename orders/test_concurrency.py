"""Exercise concurrent connections against a real file-backed test database."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

from django.core.exceptions import ValidationError
from django.db import connection, connections, transaction
from django.test import TransactionTestCase

from inventory.models import Ingredient, IngredientCategory, Recipe, RecipeIngredient, StockTransaction
from inventory.services import reserve_stock_for_order
from menu.models import Category, MenuItem
from orders.models import Order, OrderItem
from orders.services import transition_order_status
from restaurant.models import Restaurant


class ConcurrentOrderInventoryTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor == "sqlite" and connection.is_in_memory_db():
            self.skipTest("SQLite concurrency requires a file-backed TEST database; see ORDER_FLOW_AUDIT.md.")
        self.restaurant = Restaurant.objects.create(name="Concurrent kitchen", address="Test")
        category = Category.objects.create(restaurant=self.restaurant, name="Food")
        ingredient_category = IngredientCategory.objects.create(restaurant=self.restaurant, name="Fresh")
        self.ingredient = Ingredient.objects.create(
            restaurant=self.restaurant, category=ingredient_category,
            name="Chicken", sku="CHICKEN", base_unit=Ingredient.BaseUnit.GRAM,
            pack_size=1000, current_stock=150,
        )
        self.item = MenuItem.objects.create(category=category, name="Chicken dish", price=100)
        recipe = Recipe.objects.create(menu_item=self.item)
        RecipeIngredient.objects.create(recipe=recipe, ingredient=self.ingredient, quantity=100)

    def make_order(self):
        order = Order.objects.create(restaurant=self.restaurant, order_type="TAKEAWAY", total_amount=100)
        OrderItem.objects.create(order=order, menu_item=self.item, quantity=1, price=100)
        return order

    def test_two_checkouts_cannot_reserve_the_same_remaining_stock(self):
        barrier = Barrier(2)

        def checkout():
            try:
                barrier.wait(timeout=10)
                try:
                    with transaction.atomic():
                        order = self.make_order()
                        reserve_stock_for_order(order)
                    return "created"
                except ValidationError:
                    return "insufficient"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(checkout) for _ in range(2)]
            results = [future.result(timeout=30) for future in futures]

        self.assertCountEqual(results, ["created", "insufficient"])
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(OrderItem.objects.count(), 1)
        self.ingredient.refresh_from_db()
        self.assertEqual(self.ingredient.current_stock, Decimal("150"))
        self.assertEqual(self.ingredient.reserved_stock, Decimal("100"))
        self.assertEqual(self.ingredient.available_stock, Decimal("50"))
        self.assertEqual(StockTransaction.objects.count(), 0)

    def test_two_preparing_requests_consume_once(self):
        with transaction.atomic():
            order = self.make_order()
            reserve_stock_for_order(order)
            transition_order_status(order, "ACCEPTED")
        barrier = Barrier(2)

        def prepare():
            try:
                stale_order = Order.objects.get(pk=order.pk)
                barrier.wait(timeout=10)
                return transition_order_status(stale_order, "PREPARING").status
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(prepare) for _ in range(2)]
            results = [future.result(timeout=30) for future in futures]

        self.assertEqual(results, ["PREPARING", "PREPARING"])
        order.refresh_from_db()
        self.ingredient.refresh_from_db()
        self.assertEqual(order.status, "PREPARING")
        self.assertEqual(self.ingredient.current_stock, Decimal("50"))
        self.assertEqual(self.ingredient.reserved_stock, 0)
        self.assertEqual(list(order.stock_reservations.values_list("status", flat=True)), ["CONSUMED"])
        self.assertEqual(order.stock_transactions.count(), 1)
        self.assertEqual(order.stock_transactions.get().quantity, Decimal("100"))
