from decimal import Decimal
import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from inventory.models import (
    Ingredient,
    IngredientCategory,
    Recipe,
    RecipeIngredient,
)
from menu.models import Category, MenuItem
from menu.views import get_cart_key
from orders.models import Order, OrderItem
from orders.services import transition_order_status
from orders.views import _process_payment
from restaurant.models import Floor, Restaurant, Table

User = get_user_model()


class TableOccupancyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.restaurant = Restaurant.objects.create(
            name="Occupancy Test Diner", address="Dhaka"
        )
        cls.floor = Floor.objects.create(
            restaurant=cls.restaurant, name="Ground Floor", floor_number=1
        )
        cls.table = Table.objects.create(
            restaurant=cls.restaurant,
            floor=cls.floor,
            table_number=12,
            capacity=4,
            status=Table.STATUS_AVAILABLE,
            qr_code="test-fixtures/table12.png",
        )
        cls.manager = User.objects.create_user(
            username="table_mgr",
            email="mgr@example.com",
            password="password123",
            role="manager",
            restaurant=cls.restaurant,
            is_active_staff=True,
        )

        cls.category = Category.objects.create(
            name="Burgers", restaurant=cls.restaurant
        )
        ing_cat = IngredientCategory.objects.create(
            restaurant=cls.restaurant, name="Kitchen"
        )
        bun = Ingredient.objects.create(
            restaurant=cls.restaurant,
            category=ing_cat,
            name="Brioche Bun",
            sku="OCC-BUN",
            base_unit=Ingredient.BaseUnit.GRAM,
            pack_size=1000,
            current_stock=2000,
        )
        cls.item = MenuItem.objects.create(
            name="Classic Cheeseburger",
            category=cls.category,
            price=Decimal("150.00"),
            is_available=True,
        )
        recipe = Recipe.objects.create(menu_item=cls.item, yield_quantity=1)
        RecipeIngredient.objects.create(
            recipe=recipe, ingredient=bun, quantity=Decimal("50")
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.manager)

    def test_model_mark_occupied_and_close_service(self):
        table = self.table
        self.assertFalse(table.is_occupied)
        self.assertEqual(table.status, Table.STATUS_AVAILABLE)

        table.mark_occupied()
        table.refresh_from_db()
        self.assertTrue(table.is_occupied)
        self.assertEqual(table.status, Table.STATUS_OCCUPIED)

        table.close_service()
        table.refresh_from_db()
        self.assertFalse(table.is_occupied)
        self.assertEqual(table.status, Table.STATUS_AVAILABLE)

    def test_pos_dine_in_order_marks_table_occupied(self):
        payload = {
            "order_type": "DINE_IN",
            "table_id": self.table.id,
            "items": [{"id": self.item.id, "quantity": 1}],
            "payment_timing": "PAY_LATER",
        }
        resp = self.client.post(
            reverse("create_pos_order"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.table.refresh_from_db()
        self.assertTrue(self.table.is_occupied)
        self.assertEqual(self.table.status, Table.STATUS_OCCUPIED)

    def test_qr_dine_in_order_marks_table_occupied(self):
        session = self.client.session
        cart_key = get_cart_key(self.restaurant.pk, self.table.pk)
        session[cart_key] = {str(self.item.id): {"quantity": 2}}
        session.save()

        resp = self.client.post(
            reverse("checkout", args=[self.restaurant.pk, self.table.pk]),
            data={
                "order_type": "DINE_IN",
                "customer_name": "Rahim",
                "customer_phone": "01700000000",
                "payment_timing": "PAY_LATER",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.table.refresh_from_db()
        self.assertTrue(self.table.is_occupied)
        self.assertEqual(self.table.status, Table.STATUS_OCCUPIED)

    def test_existing_guest_can_place_multiple_orders_on_occupied_table(self):
        # First order
        self.table.mark_occupied()
        self.table.refresh_from_db()
        self.assertTrue(self.table.is_occupied)

        # Place second order on already-occupied table via POS
        payload = {
            "order_type": "DINE_IN",
            "table_id": self.table.id,
            "items": [{"id": self.item.id, "quantity": 1}],
            "payment_timing": "PAY_LATER",
        }
        resp = self.client.post(
            reverse("create_pos_order"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        orders = Order.objects.filter(table=self.table)
        self.assertEqual(orders.count(), 1)

        # Place third order
        resp2 = self.client.post(
            reverse("create_pos_order"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(Order.objects.filter(table=self.table).count(), 2)

    def test_completing_paid_service_releases_table_before_cleaning(self):
        from inventory.services import reserve_stock_for_order

        # Create paid order and transition to COMPLETED
        order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_type="DINE_IN",
            payment_status="PAID",
            payment_method="CASH",
            subtotal=Decimal("150.00"),
            total_amount=Decimal("150.00"),
        )
        OrderItem.objects.create(
            order=order,
            menu_item=self.item,
            quantity=1,
            price=Decimal("150.00"),
        )
        reserve_stock_for_order(order)
        self.table.mark_occupied()

        transition_order_status(order, "ACCEPTED")
        transition_order_status(order, "PREPARING")
        transition_order_status(order, "READY")
        transition_order_status(order, "SERVED")
        transition_order_status(order, "COMPLETED")
        self.assertEqual(order.status, "COMPLETED")

        self.table.refresh_from_db()
        self.assertFalse(self.table.is_occupied)
        self.assertEqual(self.table.status, Table.STATUS_AVAILABLE)
        order.table_session.refresh_from_db()
        self.assertTrue(order.table_session.clean_needed)

    def test_close_pos_table_blocked_when_unpaid_orders_exist(self):
        self.table.mark_occupied()
        Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_type="DINE_IN",
            payment_status="UNPAID",
            total_amount=Decimal("150.00"),
        )

        resp = self.client.post(reverse("close_pos_table", args=[self.table.id]))
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertFalse(data["success"])
        self.assertIn("unpaid", data["message"].lower())

        self.table.refresh_from_db()
        self.assertTrue(self.table.is_occupied)

    def test_close_pos_table_succeeds_when_all_orders_paid(self):
        self.table.mark_occupied()
        Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_type="DINE_IN",
            payment_status="PAID",
            payment_method="CASH",
            status="COMPLETED",
            total_amount=Decimal("150.00"),
        )

        resp = self.client.post(reverse("close_pos_table", args=[self.table.id]))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])

        self.table.refresh_from_db()
        self.assertFalse(self.table.is_occupied)
        self.assertEqual(self.table.status, Table.STATUS_AVAILABLE)

    def test_table_close_service_view_in_management(self):
        self.client.force_login(self.manager)
        self.table.mark_occupied()

        # Unpaid order exists -> blocked
        order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_type="DINE_IN",
            payment_status="UNPAID",
            total_amount=Decimal("150.00"),
        )
        resp = self.client.post(
            reverse("restaurant:table_close_service", args=[self.table.id])
        )
        self.assertEqual(resp.status_code, 302)
        self.table.refresh_from_db()
        self.assertTrue(self.table.is_occupied)

        # Mark paid -> succeeds
        order.payment_status = "PAID"
        order.payment_method = "CASH"
        order.save()

        resp2 = self.client.post(
            reverse("restaurant:table_close_service", args=[self.table.id])
        )
        self.assertEqual(resp2.status_code, 302)
        self.table.refresh_from_db()
        self.assertFalse(self.table.is_occupied)
        self.assertEqual(self.table.status, Table.STATUS_AVAILABLE)

    def test_process_payment_close_table_flag(self):
        from inventory.services import reserve_stock_for_order
        self.table.mark_occupied()
        order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_type="DINE_IN",
            payment_status="UNPAID",
            subtotal=Decimal("150.00"),
            total_amount=Decimal("150.00"),
        )
        OrderItem.objects.create(
            order=order,
            menu_item=self.item,
            quantity=1,
            price=Decimal("150.00"),
        )
        reserve_stock_for_order(order)
        transition_order_status(order, "ACCEPTED")
        transition_order_status(order, "PREPARING")
        transition_order_status(order, "READY")
        transition_order_status(order, "SERVED")

        # Pay with close_table=True
        err = _process_payment(order, "CASH", close_table=True)
        self.assertIsNone(err)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "PAID")
        self.assertEqual(order.status, "COMPLETED")

        # Table should now be closed and AVAILABLE
        self.table.refresh_from_db()
        self.assertFalse(self.table.is_occupied)
        self.assertEqual(self.table.status, Table.STATUS_AVAILABLE)

    def test_table_management_view_display_status_and_summary(self):
        self.client.force_login(self.manager)
        self.table.mark_occupied()

        resp = self.client.get(reverse("restaurant:table_management"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("summary", resp.context)
        self.assertEqual(resp.context["summary"]["occupied"], 1)
        self.assertContains(resp, "status-OCCUPIED")

    def test_stale_occupied_table_auto_heals_in_pos_and_table_management(self):
        from datetime import datetime, timedelta
        from unittest.mock import patch
        from django.utils import timezone

        now = timezone.make_aware(datetime(2026, 9, 18, 10))
        with patch("django.utils.timezone.now", return_value=now):
            self.table.mark_occupied()
            order = Order.objects.create(
                restaurant=self.restaurant,
                table=self.table,
                order_type="DINE_IN",
                payment_status="UNPAID",
                status="SERVED",
                total_amount=Decimal("150.00"),
            )
            # Make order from yesterday
            Order.objects.filter(pk=order.pk).update(created_at=now - timedelta(days=1))

            # Visiting POS dashboard auto-heals stale occupied table
            pos_resp = self.client.get(reverse("pos_dashboard"))
            self.assertEqual(pos_resp.status_code, 200)
            self.table.refresh_from_db()
            self.assertFalse(self.table.is_occupied)
            self.assertEqual(self.table.status, Table.STATUS_AVAILABLE)

            # Re-occupy table with stale order
            self.table.mark_occupied()
            self.client.force_login(self.manager)
            mgmt_resp = self.client.get(reverse("restaurant:table_management"))
            self.assertEqual(mgmt_resp.status_code, 200)
            self.table.refresh_from_db()
            self.assertFalse(self.table.is_occupied)
            self.assertEqual(self.table.status, Table.STATUS_AVAILABLE)

    def test_stale_unpaid_order_does_not_block_closing_table_with_paid_today_order(self):
        from datetime import datetime, timedelta
        from unittest.mock import patch
        from django.utils import timezone

        now = timezone.make_aware(datetime(2026, 9, 18, 10))
        with patch("django.utils.timezone.now", return_value=now):
            self.table.mark_occupied()
            # Stale order from yesterday
            stale_order = Order.objects.create(
                restaurant=self.restaurant,
                table=self.table,
                order_type="DINE_IN",
                payment_status="UNPAID",
                status="SERVED",
                total_amount=Decimal("150.00"),
            )
            Order.objects.filter(pk=stale_order.pk).update(created_at=now - timedelta(days=1))

            # Today's order, completed & paid
            today_order = Order.objects.create(
                restaurant=self.restaurant,
                table=self.table,
                order_type="DINE_IN",
                payment_status="PAID",
                payment_method="CASH",
                status="COMPLETED",
                total_amount=Decimal("150.00"),
            )

            # close_pos_table should ignore yesterday's order and succeed
            resp = self.client.post(reverse("close_pos_table", args=[self.table.id]))
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["success"])
            self.table.refresh_from_db()
            self.assertFalse(self.table.is_occupied)
            self.assertEqual(self.table.status, Table.STATUS_AVAILABLE)
