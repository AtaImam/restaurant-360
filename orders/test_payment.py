"""Tests for POS + Billing + Payment flow.

Covers:
  - PAY_NOW POS: payment recorded atomically, order PAID, redirect to receipt
  - PAY_LATER POS: order UNPAID, redirect to bill preview
  - PAY_NOW QR: order PAID, redirect to receipt
  - PAY_LATER QR: order UNPAID, redirect to order_success
  - SERVED+UNPAID cannot transition to COMPLETED
  - SERVED+PAID can transition to COMPLETED (payment guard only, no inventory)
  - Invalid payment method rejected by pay_order and create_pos_order
  - bill_preview GET renders correctly, POST pays and redirects
  - order_receipt GET renders correctly
  - pay_order JSON endpoint records payment
"""

import json
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from inventory.models import (
    Ingredient,
    IngredientCategory,
    Recipe,
    RecipeIngredient,
)
from menu.models import Category, MenuItem
from users.models import User
from django.utils import timezone
from kitchen.views import get_live_order_cutoff
from staff.models import EmployeeProfile, OrderStaffService
from staff.operations import ensure_order_staff_service, register_new_order
from orders.views import _table_service_summary
from menu.views import get_cart_key
from orders.models import DiningSession, Order, OrderItem, TableSession
from orders.services import transition_order_status
from restaurant.models import Restaurant, Table


# ---------------------------------------------------------------------------
# Base fixture — mirrors OrderFlowFixture from test_flow.py
# ---------------------------------------------------------------------------

class PaymentFlowFixture:
    """Creates real ingredient + recipe data so reserve_stock_for_order works."""

    @classmethod
    def setUpTestData(cls):
        cls.restaurant = Restaurant.objects.create(
            name="Pay Test Restaurant", address="Dhaka"
        )
        cls.table = Table.objects.create(
            restaurant=cls.restaurant,
            table_number=1,
            qr_code="test-fixtures/table.png",
        )
        cls.category = Category.objects.create(
            name="Mains", restaurant=cls.restaurant
        )
        # Staff user required now that back-office views enforce login
        cls.staff_user = User.objects.create_user(
            username="paytest_staff",
            password="testpass",
            role="manager",
            restaurant=cls.restaurant,
            is_superuser=True,
        )

        # Ingredients
        ing_cat = IngredientCategory.objects.create(
            restaurant=cls.restaurant, name="Kitchen"
        )
        cls.chicken = Ingredient.objects.create(
            restaurant=cls.restaurant,
            category=ing_cat,
            name="Chicken",
            sku="PTEST-CHICKEN",
            base_unit=Ingredient.BaseUnit.GRAM,
            pack_size=1000,
            current_stock=2000,
        )

        # Menu item with recipe
        cls.item = MenuItem.objects.create(
            name="Burger",
            category=cls.category,
            price=Decimal("100.00"),
            is_available=True,
        )
        recipe = Recipe.objects.create(menu_item=cls.item, yield_quantity=1)
        RecipeIngredient.objects.create(
            recipe=recipe, ingredient=cls.chicken, quantity=Decimal("50")
        )

    def seed_cart(self, quantity=1):
        session = self.client.session
        key = get_cart_key(self.restaurant.pk, self.table.pk)
        session[key] = {
            str(self.item.pk): {
                "name": self.item.name,
                "price": str(self.item.price),
                "quantity": quantity,
            }
        }
        session.save()
        return key

    def pos_checkout(self, timing="PAY_LATER", method=None, quantity=1):
        payload = {
            "order_type": "DINE_IN",
            "table_id": self.table.id,
            "items": [{"id": self.item.id, "quantity": quantity}],
            "discount_amount": 0,
            "service_percent": 0,
            "vat_percent": 0,
            "payment_timing": timing,
        }
        if method is not None:
            payload["payment_method"] = method
        return self.client.post(
            reverse("create_pos_order"),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def qr_checkout(self, timing="PAY_LATER", method=None):
        self.seed_cart()
        post_data = {
            "order_type": "DINE_IN",
            "payment_timing": timing,
        }
        if method is not None:
            post_data["payment_method"] = method
        return self.client.post(
            reverse("checkout", args=[self.restaurant.pk, self.table.pk]),
            post_data,
        )


# ---------------------------------------------------------------------------
# Payment guard: SERVED+UNPAID cannot become COMPLETED
# ---------------------------------------------------------------------------

class PaymentGuardTest(TestCase):
    """The transition service blocks SERVED+UNPAID → COMPLETED."""

    def setUp(self):
        self.restaurant = Restaurant.objects.create(
            name="Guard Test", address="Test"
        )
        self.table = Table.objects.create(
            restaurant=self.restaurant, table_number=1
        )

    def _make_order(self, payment_status="UNPAID"):
        order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_type="DINE_IN",
            status="SERVED",
            subtotal="100.00",
            discount_amount="0.00",
            service_charge="0.00",
            vat_amount="0.00",
            total_amount="100.00",
            payment_status=payment_status,
        )
        return order

    def test_served_unpaid_cannot_complete(self):
        order = self._make_order("UNPAID")
        with self.assertRaises(ValidationError) as ctx:
            transition_order_status(order, "COMPLETED")
        self.assertIn("not been paid", str(ctx.exception))

    def test_served_unpaid_error_message_is_helpful(self):
        order = self._make_order("UNPAID")
        try:
            transition_order_status(order, "COMPLETED")
            self.fail("Expected ValidationError")
        except ValidationError as exc:
            msg = " ".join(exc.messages)
            self.assertIn("payment", msg.lower())

    def test_invalid_next_status_still_blocked(self):
        """Unrelated invalid transition is still blocked (guard doesn't break other checks)."""
        order = self._make_order("PAID")
        order.status = "NEW"
        order.save(update_fields=["status"])
        with self.assertRaises(ValidationError):
            transition_order_status(order, "COMPLETED")  # NEW → COMPLETED is invalid


# ---------------------------------------------------------------------------
# pay_order JSON endpoint
# ---------------------------------------------------------------------------

class PayOrderEndpointTest(TestCase):
    """pay_order records payment and returns receipt URL."""

    def setUp(self):
        self.client = Client()
        self.restaurant = Restaurant.objects.create(
            name="Pay Endpoint Test", address="Test"
        )
        self.staff_user = User.objects.create_user(
            username="payendpoint_staff", password="tp",
            role="manager", restaurant=self.restaurant, is_superuser=True,
        )
        self.client.force_login(self.staff_user)
        self.table = Table.objects.create(
            restaurant=self.restaurant, table_number=1
        )
        self.order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_type="DINE_IN",
            status="NEW",
            subtotal="100.00",
            discount_amount="0.00",
            service_charge="0.00",
            vat_amount="0.00",
            total_amount="100.00",
            payment_status="UNPAID",
        )

    def _post_pay(self, method, **extra):
        url = reverse("pay_order", args=[self.order.id])
        return self.client.post(
            url,
            data=json.dumps({"payment_method": method, **extra}),
            content_type="application/json",
        )

    def test_pay_cash(self):
        response = self._post_pay("CASH")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertIn("receipt_url", data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "PAID")
        self.assertEqual(self.order.payment_method, "CASH")

    def test_pay_card(self):
        response = self._post_pay("CARD")
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_method, "CARD")

    def test_pay_mobile(self):
        response = self._post_pay("MOBILE_BANKING")
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_method, "MOBILE_BANKING")

    def test_invalid_method_rejected(self):
        response = self._post_pay("BITCOIN")
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data["success"])

    def test_already_paid_idempotent(self):
        self.order.payment_status = "PAID"
        self.order.payment_method = "CASH"
        self.order.save(update_fields=["payment_status", "payment_method"])
        response = self._post_pay("CARD")
        # Already paid — idempotent, returns success.
        self.assertEqual(response.status_code, 200)
        # Payment method should NOT have changed.
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_method, "CASH")

    def test_missing_body_returns_400(self):
        url = reverse("pay_order", args=[self.order.id])
        response = self.client.post(url, data="not json", content_type="application/json")
        self.assertEqual(response.status_code, 400)


# ---------------------------------------------------------------------------
# bill_preview view
# ---------------------------------------------------------------------------

class BillPreviewViewTest(TestCase):
    """bill_preview GET renders; POST pays and redirects to receipt."""

    def setUp(self):
        self.client = Client()
        self.restaurant = Restaurant.objects.create(
            name="Bill Preview Test", address="Test"
        )
        self.staff_user = User.objects.create_user(
            username="billpreview_staff", password="tp",
            role="manager", restaurant=self.restaurant, is_superuser=True,
        )
        self.client.force_login(self.staff_user)
        self.table = Table.objects.create(
            restaurant=self.restaurant, table_number=1
        )
        self.order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_type="DINE_IN",
            status="NEW",
            subtotal="100.00",
            discount_amount="0.00",
            service_charge="0.00",
            vat_amount="0.00",
            total_amount="100.00",
            payment_status="UNPAID",
        )

    def test_get_renders_bill(self):
        url = reverse("bill_preview", args=[self.order.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bill")

    def test_get_shows_payment_methods(self):
        url = reverse("bill_preview", args=[self.order.id])
        response = self.client.get(url)
        self.assertContains(response, "Cash")

    def test_post_cash_pays_and_redirects_to_receipt(self):
        url = reverse("bill_preview", args=[self.order.id])
        response = self.client.post(url, {"payment_method": "CASH"})
        self.assertIn(response.status_code, [301, 302])
        self.assertIn("receipt", response["Location"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "PAID")

    def test_post_invalid_method_shows_error(self):
        url = reverse("bill_preview", args=[self.order.id])
        response = self.client.post(url, {"payment_method": "INVALID"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "valid payment method")

    def test_get_already_paid_shows_view_receipt(self):
        self.order.payment_status = "PAID"
        self.order.payment_method = "CARD"
        self.order.save(update_fields=["payment_status", "payment_method"])
        url = reverse("bill_preview", args=[self.order.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "View Receipt")


# ---------------------------------------------------------------------------
# order_receipt view
# ---------------------------------------------------------------------------

class ReceiptViewTest(TestCase):
    """order_receipt renders print receipt with payment details."""

    def setUp(self):
        self.client = Client()
        self.restaurant = Restaurant.objects.create(
            name="Receipt Test", address="Test"
        )
        self.staff_user = User.objects.create_user(
            username="receipt_staff", password="tp",
            role="manager", restaurant=self.restaurant, is_superuser=True,
        )
        self.client.force_login(self.staff_user)
        self.table = Table.objects.create(
            restaurant=self.restaurant, table_number=1
        )
        self.order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_type="DINE_IN",
            status="NEW",
            subtotal="100.00",
            discount_amount="0.00",
            service_charge="0.00",
            vat_amount="0.00",
            total_amount="100.00",
            payment_status="PAID",
            payment_method="CARD",
        )

    def test_receipt_renders(self):
        url = reverse("order_receipt", args=[self.order.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_receipt_shows_paid_badge(self):
        url = reverse("order_receipt", args=[self.order.id])
        response = self.client.get(url)
        self.assertContains(response, "PAID")

    def test_receipt_shows_restaurant_name(self):
        url = reverse("order_receipt", args=[self.order.id])
        response = self.client.get(url)
        self.assertContains(response, "Receipt Test")


# ---------------------------------------------------------------------------
# POS PAY_NOW / PAY_LATER
# ---------------------------------------------------------------------------

class POSPaymentTimingTest(PaymentFlowFixture, TestCase):
    """POS PAY_NOW creates PAID; PAY_LATER creates UNPAID."""

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff_user)

    def test_pay_later_creates_unpaid_order(self):
        response = self.pos_checkout("PAY_LATER")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["payment_timing"], "PAY_LATER")
        order = Order.objects.get(id=data["order_id"])
        self.assertEqual(order.payment_status, "UNPAID")
        self.assertEqual(order.payment_method, "")
        self.assertIsNone(data["redirect_url"])

    def test_pay_later_no_redirect_url_returns_clean_pos(self):
        response = self.pos_checkout("PAY_LATER")
        data = response.json()
        self.assertIsNone(data["redirect_url"])
        self.assertIn("KOT", data["message"])

    def test_pay_now_cash_creates_paid_order(self):
        response = self.pos_checkout("PAY_NOW", method="CASH")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        order = Order.objects.get(id=data["order_id"])
        self.assertEqual(order.payment_status, "PAID")
        self.assertEqual(order.payment_method, "CASH")

    def test_pay_now_redirect_is_receipt(self):
        response = self.pos_checkout("PAY_NOW", method="CARD")
        data = response.json()
        expected_url = reverse("order_receipt", args=[data["order_id"]])
        self.assertEqual(data["redirect_url"], expected_url)

    def test_pay_now_without_method_returns_400(self):
        response = self.pos_checkout("PAY_NOW", method=None)
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data["success"])
        self.assertIn("payment method", data["message"].lower())

    def test_pay_now_invalid_method_returns_400(self):
        response = self.pos_checkout("PAY_NOW", method="PAYPAL")
        self.assertEqual(response.status_code, 400)

    def test_pay_now_mobile_banking(self):
        response = self.pos_checkout("PAY_NOW", method="MOBILE_BANKING")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        order = Order.objects.get(id=data["order_id"])
        self.assertEqual(order.payment_method, "MOBILE_BANKING")


# ---------------------------------------------------------------------------
# QR Checkout PAY_NOW / PAY_LATER
# ---------------------------------------------------------------------------

class QRCheckoutPaymentTimingTest(PaymentFlowFixture, TestCase):
    """QR checkout: PAY_NOW → order_success; PAY_LATER → order_success (anonymous)."""

    def setUp(self):
        self.client = Client()
        # Anonymous customer: never logged in!

    def test_pay_later_creates_unpaid_redirects_to_success(self):
        response = self.qr_checkout("PAY_LATER")
        self.assertIn(response.status_code, [301, 302])
        order = Order.objects.filter(restaurant=self.restaurant).last()
        self.assertEqual(order.payment_status, "UNPAID")
        self.assertIn("success", response["Location"])

    def test_pay_now_creates_paid_redirects_to_success(self):
        response = self.qr_checkout("PAY_NOW", method="CARD")
        self.assertIn(response.status_code, [301, 302])
        order = Order.objects.filter(restaurant=self.restaurant).last()
        self.assertEqual(order.payment_status, "PAID")
        self.assertEqual(order.payment_method, "CARD")
        self.assertIn("success", response["Location"])

    def test_pay_now_cash_creates_paid_order(self):
        response = self.qr_checkout("PAY_NOW", method="CASH")
        self.assertIn(response.status_code, [301, 302])
        order = Order.objects.filter(restaurant=self.restaurant).last()
        self.assertEqual(order.payment_method, "CASH")

    def test_pay_now_without_method_shows_checkout_error(self):
        # Need to re-seed cart for this request (qr_checkout also seeds)
        self.seed_cart()
        response = self.client.post(
            reverse("checkout", args=[self.restaurant.pk, self.table.pk]),
            {"order_type": "DINE_IN", "payment_timing": "PAY_NOW"},  # No method
        )
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "payment method", status_code=400)

    def test_pay_now_invalid_method_shows_checkout_error(self):
        self.seed_cart()
        response = self.client.post(
            reverse("checkout", args=[self.restaurant.pk, self.table.pk]),
            {
                "order_type": "DINE_IN",
                "payment_timing": "PAY_NOW",
                "payment_method": "BITCOIN",
            },
        )
        self.assertEqual(response.status_code, 400)


# ---------------------------------------------------------------------------
# SERVED order payment collection + auto-completion
# ---------------------------------------------------------------------------

class ServedOrderPaymentCompletionTest(PaymentFlowFixture, TestCase):
    """Paying a SERVED order auto-completes it."""

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff_user)
        self.order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_type="DINE_IN",
            status="NEW",
            subtotal=Decimal("100.00"),
            discount_amount=Decimal("0.00"),
            service_charge=Decimal("0.00"),
            vat_amount=Decimal("0.00"),
            total_amount=Decimal("100.00"),
            payment_status="UNPAID",
        )
        OrderItem.objects.create(
            order=self.order,
            menu_item=self.item,
            quantity=1,
            price=self.item.price,
        )
        from inventory.services import reserve_stock_for_order
        reserve_stock_for_order(self.order)
        transition_order_status(self.order, "ACCEPTED")
        transition_order_status(self.order, "PREPARING")
        transition_order_status(self.order, "READY")
        transition_order_status(self.order, "SERVED")

    def test_collect_payment_on_served_order_auto_completes(self):
        url = reverse("bill_preview", args=[self.order.id])
        response = self.client.post(url, {"payment_method": "CARD"})
        self.assertIn(response.status_code, [301, 302])
        self.assertIn("receipt", response["Location"])

        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "PAID")
        self.assertEqual(self.order.payment_method, "CARD")
        self.assertEqual(self.order.status, "COMPLETED")

    def test_pay_order_json_on_served_order_auto_completes(self):
        url = reverse("pay_order", args=[self.order.id])
        response = self.client.post(
            url,
            data=json.dumps({"payment_method": "MOBILE_BANKING"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])

        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "PAID")
        self.assertEqual(self.order.payment_method, "MOBILE_BANKING")
        self.assertEqual(self.order.status, "COMPLETED")

    def test_payment_on_new_order_does_not_prematurely_complete(self):
        """Payment on an unserved order marks PAID but leaves status unchanged."""
        new_order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_type="DINE_IN",
            status="NEW",
            subtotal="100.00",
            discount_amount="0.00",
            service_charge="0.00",
            vat_amount="0.00",
            total_amount="100.00",
            payment_status="UNPAID",
        )
        url = reverse("bill_preview", args=[new_order.id])
        self.client.post(url, {"payment_method": "CASH"})

        new_order.refresh_from_db()
        self.assertEqual(new_order.payment_status, "PAID")
        self.assertEqual(new_order.status, "NEW")


# ---------------------------------------------------------------------------
# Dashboard order_detail: Collect Payment vs Complete Order
# ---------------------------------------------------------------------------

class DashboardOrderDetailBillingFlowTest(TestCase):
    """SERVED + UNPAID shows Collect Payment; SERVED + PAID shows Complete Order."""

    def setUp(self):
        self.client = Client()
        self.restaurant = Restaurant.objects.create(
            name="Dashboard Billing Test", address="Dhaka"
        )
        self.staff_user = User.objects.create_user(
            username="dashbilling_staff", password="tp",
            role="manager", restaurant=self.restaurant, is_superuser=True,
        )
        self.client.force_login(self.staff_user)
        self.table = Table.objects.create(
            restaurant=self.restaurant, table_number=2
        )

    def test_served_unpaid_shows_collect_payment_not_complete_order(self):
        order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_type="DINE_IN",
            status="SERVED",
            subtotal="150.00",
            discount_amount="0.00",
            service_charge="0.00",
            vat_amount="0.00",
            total_amount="150.00",
            payment_status="UNPAID",
        )
        url = reverse("order_detail", args=[order.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        # Context checks
        self.assertTrue(response.context["show_collect_payment"])
        self.assertEqual(response.context["status_choices"], [])

        # Content checks: Collect Payment button present, Complete Order button absent
        self.assertContains(response, "Collect Payment")
        self.assertNotContains(response, "Complete Order")

        # Attempting to POST status=COMPLETED is rejected
        post_resp = self.client.post(url, {"status": "COMPLETED"}, follow=True)
        order.refresh_from_db()
        self.assertEqual(order.status, "SERVED")
        self.assertContains(post_resp, "not been paid")

    def test_served_paid_shows_complete_order_and_receipt(self):
        order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_type="DINE_IN",
            status="SERVED",
            subtotal="150.00",
            discount_amount="0.00",
            service_charge="0.00",
            vat_amount="0.00",
            total_amount="150.00",
            payment_status="PAID",
            payment_method="CASH",
        )
        url = reverse("order_detail", args=[order.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        # Context checks
        self.assertFalse(response.context["show_collect_payment"])
        self.assertEqual(
            response.context["status_choices"],
            [("COMPLETED", "Complete Order")],
        )

        # Content checks: Complete Order present, Collect Payment absent
        self.assertContains(response, "Complete Order")
        self.assertNotContains(response, "Collect Payment")
        self.assertContains(response, "Printable Receipt")


class TableSessionBillingAndAuditTests(PaymentFlowFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff_user)
        self.table = Table.objects.create(
            restaurant=self.restaurant,
            table_number=5,
            capacity=4,
            status=Table.STATUS_AVAILABLE,
        )

    def _create_served_order(self, quantity=1):
        order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_type="DINE_IN",
            status="NEW",
            subtotal=self.item.price * quantity,
            discount_amount=Decimal("0.00"),
            service_charge=Decimal("0.00"),
            vat_amount=Decimal("0.00"),
            total_amount=self.item.price * quantity,
            payment_status="UNPAID",
        )
        OrderItem.objects.create(
            order=order,
            menu_item=self.item,
            quantity=quantity,
            price=self.item.price,
        )
        from inventory.services import reserve_stock_for_order
        reserve_stock_for_order(order)
        transition_order_status(order, "ACCEPTED")
        transition_order_status(order, "PREPARING")
        transition_order_status(order, "READY")
        transition_order_status(order, "SERVED")
        return order

    def test_table_service_summary_and_full_table_settlement(self):
        order1 = self._create_served_order(quantity=1)
        order2 = self._create_served_order(quantity=2)
        self.table.mark_occupied()
        self.assertEqual(self.table.status, Table.STATUS_OCCUPIED)

        summary = _table_service_summary(order1)
        self.assertEqual(summary["orders_count"], 2)
        self.assertEqual(summary["total_amount"], Decimal("300.00"))
        self.assertEqual(summary["paid_amount"], Decimal("0.00"))
        self.assertEqual(summary["unpaid_amount"], Decimal("300.00"))

        # Bill preview shows full table summary
        url = reverse("bill_preview", args=[order2.id])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Table Dining Session")
        self.assertContains(resp, "300.00")

        # Pay full table with settle_scope="TABLE"
        pay_resp = self.client.post(
            url,
            {
                "settle_scope": "TABLE",
                "payment_method": "CASH",
                "payment_reference": "TXN-TABLE-555",
                "free_table": "1",
            },
            follow=True,
        )
        self.assertEqual(pay_resp.status_code, 200)

        order1.refresh_from_db()
        order2.refresh_from_db()
        self.table.refresh_from_db()

        self.assertEqual(order1.payment_status, "PAID")
        self.assertEqual(order1.payment_method, "CASH")
        self.assertEqual(order1.payment_reference, "TXN-TABLE-555")
        self.assertEqual(order1.status, "COMPLETED")

        self.assertEqual(order2.payment_status, "PAID")
        self.assertEqual(order2.payment_method, "CASH")
        self.assertEqual(order2.payment_reference, "TXN-TABLE-555")
        self.assertEqual(order2.status, "COMPLETED")

        # Table is freed because all orders settled
        self.assertEqual(self.table.status, Table.STATUS_AVAILABLE)

    def test_single_order_payment_cannot_free_table_with_other_unpaid(self):
        order1 = self._create_served_order(quantity=1)
        order2 = self._create_served_order(quantity=1)
        self.table.mark_occupied()

        # Pay only order2 with free_table attempted
        url = reverse("bill_preview", args=[order2.id])
        resp = self.client.post(
            url,
            {
                "settle_scope": "ORDER",
                "payment_method": "CARD",
                "payment_reference": "CARD-8888",
                "free_table": "1",
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)

        order1.refresh_from_db()
        order2.refresh_from_db()
        self.table.refresh_from_db()

        self.assertEqual(order2.payment_status, "PAID")
        self.assertEqual(order2.status, "COMPLETED")
        self.assertEqual(order1.payment_status, "UNPAID")
        self.assertEqual(order1.status, "SERVED")

        # Table MUST NOT be freed while order1 is unpaid!
        self.assertEqual(self.table.status, Table.STATUS_OCCUPIED)

    def test_waiter_linkage_inheritance_across_table_session(self):
        waiter_user = User.objects.create_user(
            username="waiter_audit",
            role="waiter",
            restaurant=self.restaurant,
        )
        emp_profile = EmployeeProfile.objects.create(
            user=waiter_user,
            employee_id="EMP-WAITER-01",
            joining_date=timezone.localdate(),
        )

        order1 = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_type="DINE_IN",
            status="NEW",
            subtotal=Decimal("100.00"),
            discount_amount=Decimal("0.00"),
            service_charge=Decimal("0.00"),
            vat_amount=Decimal("0.00"),
            total_amount=Decimal("100.00"),
        )
        register_new_order(order1.pk, waiter_user=waiter_user)
        self.assertEqual(order1.staff_service.waiter, emp_profile)

        # Order 2 placed on same table (e.g. customer QR order, no waiter specified)
        order2 = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_type="DINE_IN",
            status="NEW",
            subtotal=Decimal("50.00"),
            discount_amount=Decimal("0.00"),
            service_charge=Decimal("0.00"),
            vat_amount=Decimal("0.00"),
            total_amount=Decimal("50.00"),
        )
        register_new_order(order2.pk)
        self.assertEqual(order2.staff_service.waiter, emp_profile)

    def test_kitchen_dashboard_excludes_cancelled_and_stale_orders(self):
        now = timezone.now()

        live_new = Order.objects.create(
            restaurant=self.restaurant, table=self.table, order_type="DINE_IN", status="NEW",
            subtotal=10, discount_amount=0, service_charge=0, vat_amount=0, total_amount=10
        )
        live_prep = Order.objects.create(
            restaurant=self.restaurant, table=self.table, order_type="DINE_IN", status="PREPARING",
            subtotal=10, discount_amount=0, service_charge=0, vat_amount=0, total_amount=10
        )
        live_cancelled = Order.objects.create(
            restaurant=self.restaurant, table=self.table, order_type="DINE_IN", status="CANCELLED",
            subtotal=10, discount_amount=0, service_charge=0, vat_amount=0, total_amount=10
        )
        stale_order = Order.objects.create(
            restaurant=self.restaurant, table=self.table, order_type="DINE_IN", status="NEW",
            subtotal=10, discount_amount=0, service_charge=0, vat_amount=0, total_amount=10
        )
        Order.objects.filter(pk=stale_order.pk).update(created_at=now - timezone.timedelta(days=2))

        resp = self.client.get(reverse("kitchen_dashboard"))
        self.assertEqual(resp.status_code, 200)
        orders_in_kitchen = list(resp.context["orders"])
        self.assertIn(live_new, orders_in_kitchen)
        self.assertIn(live_prep, orders_in_kitchen)
        self.assertNotIn(live_cancelled, orders_in_kitchen)
        self.assertNotIn(stale_order, orders_in_kitchen)

    def test_same_table_multi_order_belongs_to_same_open_session(self):
        # First order places table into open session
        order1 = self._create_served_order(quantity=1)
        self.assertIsNotNone(order1.table_session)
        session = order1.table_session
        self.assertTrue(session.is_open)
        self.assertEqual(session.status, TableSession.STATUS_OPEN)

        # Second order on the same table automatically joins the same session
        order2 = self._create_served_order(quantity=2)
        self.assertEqual(order2.table_session, session)
        self.assertEqual(session.orders.count(), 2)

        # Table billing aggregates both orders from this session
        summary = _table_service_summary(order2)
        self.assertEqual(summary["session"], session)
        self.assertEqual(summary["orders_count"], 2)
        self.assertEqual(summary["total_amount"], Decimal("300.00"))

    def test_table_close_marks_session_closed_and_table_available(self):
        order = self._create_served_order(quantity=1)
        session = order.table_session
        self.table.mark_occupied()
        self.assertEqual(self.table.status, Table.STATUS_OCCUPIED)
        self.assertTrue(session.is_open)

        # Settle order and close table
        url = reverse("bill_preview", args=[order.id])
        resp = self.client.post(
            url,
            {
                "settle_scope": "TABLE",
                "payment_method": "CASH",
                "payment_reference": "TXN-CLOSE-01",
                "close_table": "1",
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)

        session.refresh_from_db()
        self.table.refresh_from_db()
        self.assertTrue(session.is_closed)
        self.assertEqual(session.status, TableSession.STATUS_CLOSED)
        self.assertIsNotNone(session.closed_at)
        self.assertEqual(self.table.status, Table.STATUS_AVAILABLE)
        self.assertFalse(self.table.is_occupied)

    def test_same_day_second_guest_creates_new_session_and_old_session_isolated(self):
        # --- GUEST 1 ---
        guest1_order1 = self._create_served_order(quantity=1)
        guest1_order2 = self._create_served_order(quantity=1)
        session1 = guest1_order1.table_session
        self.assertEqual(guest1_order2.table_session, session1)
        self.table.mark_occupied()

        # Guest 1 pays and service is closed
        url1 = reverse("bill_preview", args=[guest1_order1.id])
        self.client.post(
            url1,
            {
                "settle_scope": "TABLE",
                "payment_method": "CASH",
                "payment_reference": "GUEST1-PAID",
                "close_table": "1",
            },
            follow=True,
        )
        session1.refresh_from_db()
        self.table.refresh_from_db()
        self.assertEqual(session1.status, TableSession.STATUS_CLOSED)
        self.assertEqual(self.table.status, Table.STATUS_AVAILABLE)
        self.assertFalse(self.table.is_occupied)

        # --- GUEST 2 (Later on the same day at the same table) ---
        guest2_order = self._create_served_order(quantity=3)
        session2 = guest2_order.table_session

        # Guest 2 MUST get a brand new session, completely separate from Guest 1
        self.assertNotEqual(session2.id, session1.id)
        self.assertEqual(session2.status, TableSession.STATUS_OPEN)
        self.table.refresh_from_db()
        self.assertEqual(self.table.status, Table.STATUS_OCCUPIED)

        # Table billing for Guest 2 must ONLY aggregate Guest 2's session
        summary2 = _table_service_summary(guest2_order)
        self.assertEqual(summary2["session"], session2)
        self.assertEqual(summary2["orders_count"], 1)
        self.assertEqual(summary2["total_amount"], Decimal("300.00"))
        self.assertEqual(summary2["unpaid_amount"], Decimal("300.00"))
        self.assertNotIn(guest1_order1, summary2["session_orders"])
        self.assertNotIn(guest1_order2, summary2["session_orders"])

        # Bill preview for Guest 2 does NOT show Guest 1's orders or total
        url2 = reverse("bill_preview", args=[guest2_order.id])
        resp2 = self.client.get(url2)
        self.assertEqual(resp2.status_code, 200)
        self.assertNotContains(resp2, f"Order #{guest1_order1.id}")
        self.assertNotContains(resp2, f"Order #{guest1_order2.id}")
        self.assertContains(resp2, f"Order #{guest2_order.id}")

    def test_old_session_does_not_affect_new_guest_occupancy_or_billing(self):
        # Create an old session with an order
        old_order = self._create_served_order(quantity=1)
        old_session = old_order.table_session
        self.table.close_service()
        old_session.refresh_from_db()
        self.assertEqual(old_session.status, TableSession.STATUS_CLOSED)
        self.assertEqual(self.table.status, Table.STATUS_AVAILABLE)

        # Ensure table reports no unpaid orders when session is closed
        self.assertFalse(self.table.has_unpaid_orders)

        # New guest service begins
        new_session = self.table.mark_occupied()
        self.assertEqual(new_session.status, TableSession.STATUS_OPEN)
        self.assertNotEqual(new_session.id, old_session.id)
        self.assertEqual(self.table.active_session, new_session)

        # New guest places an order
        new_order = self._create_served_order(quantity=2)
        self.assertEqual(new_order.table_session, new_session)
        self.assertTrue(self.table.has_unpaid_orders)

        # Pay new order and close
        url = reverse("bill_preview", args=[new_order.id])
        self.client.post(
            url,
            {
                "settle_scope": "TABLE",
                "payment_method": "CARD",
                "payment_reference": "CARD-NEW-999",
                "close_table": "1",
            },
            follow=True,
        )
        self.table.refresh_from_db()
        new_session.refresh_from_db()
        self.assertEqual(self.table.status, Table.STATUS_AVAILABLE)
        self.assertEqual(new_session.status, TableSession.STATUS_CLOSED)


    def test_last_payment_releases_without_close_flag_and_retry_preserves_new_session(self):
        first = self._create_served_order()
        second = self._create_served_order()
        url = reverse("bill_preview", args=[first.pk])
        self.client.post(url, {"settle_scope": "ORDER", "payment_method": "CASH"})
        self.table.refresh_from_db()
        self.assertEqual(self.table.status, Table.STATUS_OCCUPIED)
        self.client.post(reverse("bill_preview", args=[second.pk]), {"settle_scope": "ORDER", "payment_method": "CARD"})
        self.table.refresh_from_db()
        first.table_session.refresh_from_db()
        self.assertEqual(self.table.status, Table.STATUS_AVAILABLE)
        self.assertTrue(first.table_session.clean_needed)
        self.assertTrue(first.table_session.is_closed)
        new = self._create_served_order()
        self.assertNotEqual(new.table_session_id, first.table_session_id)
        self.client.post(url, {"settle_scope": "TABLE", "payment_method": "CASH", "close_table": "1"})
        self.table.refresh_from_db()
        new.refresh_from_db()
        self.assertEqual(self.table.status, Table.STATUS_OCCUPIED)
        self.assertEqual(new.payment_status, "UNPAID")
        self.assertEqual(self.table.active_session.pk, new.table_session_id)

    def test_prepayment_keeps_table_until_food_is_served(self):
        from orders.services import record_order_payment
        order = Order.objects.create(
            restaurant=self.restaurant, table=self.table, order_type="DINE_IN",
            total_amount=100,
        )
        record_order_payment(order)
        self.table.refresh_from_db()
        order.table_session.refresh_from_db()
        self.assertEqual(self.table.status, Table.STATUS_OCCUPIED)
        self.assertTrue(order.table_session.is_open)
        self.assertFalse(order.table_session.clean_needed)

    def test_existing_settled_sessions_are_released_by_migration(self):
        from importlib import import_module
        from django.apps import apps
        from django.db import connection
        from orders.models import TableSession

        order = self._create_served_order()
        # Simulate a paid service left occupied by the previous implementation.
        Order.objects.filter(pk=order.pk).update(payment_status="PAID", status="COMPLETED")
        migration = import_module("orders.migrations.0013_tablesession_clean_needed_tablesession_cleaned_at_and_more")
        from types import SimpleNamespace
        migration.release_existing_settled_sessions(apps, SimpleNamespace(connection=connection))
        self.table.refresh_from_db()
        session = TableSession.objects.get(pk=order.table_session_id)
        self.assertEqual(self.table.status, Table.STATUS_AVAILABLE)
        self.assertTrue(session.is_closed)
        self.assertTrue(session.clean_needed)

    def test_archiving_old_session_does_not_close_new_service(self):
        from orders.services import archive_stale_order, record_order_payment
        old = self._create_served_order()
        record_order_payment(old)
        self.table.refresh_from_db()
        new = self._create_served_order()
        archive_stale_order(old)
        self.table.refresh_from_db()
        self.assertEqual(self.table.status, Table.STATUS_OCCUPIED)
        self.assertEqual(self.table.active_session.pk, new.table_session_id)


class OrderFlowEndToEndAuditTests(PaymentFlowFixture, TestCase):
    """
    Comprehensive end-to-end audit tests for the complete Restaurant 360 order flow:
    A) QR PAY NOW anonymous
    B) QR PAY LATER anonymous
    C) Same table 2+ separate orders -> one combined bill
    D) Owner collects payment after serving
    E) Payment -> completed -> table available
    F) Receipt + feedback QR
    And regression tests for:
    - Rescanning the same table
    - Repeated payment POSTs (idempotency)
    - Mixed paid and unpaid orders
    - Cancelled orders
    - Session close conditions
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.item_a = cls.item
        cls.item_b = MenuItem.objects.create(
            name="Lassi",
            category=cls.category,
            price=Decimal("50.00"),
            is_available=True,
        )
        recipe_b = Recipe.objects.create(menu_item=cls.item_b, yield_quantity=1)
        RecipeIngredient.objects.create(
            recipe=recipe_b, ingredient=cls.chicken, quantity=Decimal("10")
        )

    def setUp(self):
        super().setUp()
        self.client = Client()

    def seed_cart(self, items=None, quantity=1):
        session = self.client.session
        key = get_cart_key(self.restaurant.pk, self.table.pk)
        cart = {}
        if items:
            for itm, qty in items:
                cart[str(itm.pk)] = {
                    "name": itm.name,
                    "price": str(itm.price),
                    "quantity": qty,
                }
        else:
            cart[str(self.item.pk)] = {
                "name": self.item.name,
                "price": str(self.item.price),
                "quantity": quantity,
            }
        session[key] = cart
        session.save()
        return key

    def _create_served_order(self, quantity=1):
        order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_type="DINE_IN",
            status="NEW",
            subtotal=self.item.price * quantity,
            discount_amount=Decimal("0.00"),
            service_charge=Decimal("0.00"),
            vat_amount=Decimal("0.00"),
            total_amount=self.item.price * quantity,
            payment_status="UNPAID",
        )
        OrderItem.objects.create(
            order=order,
            menu_item=self.item,
            quantity=quantity,
            price=self.item.price,
        )
        from inventory.services import reserve_stock_for_order
        reserve_stock_for_order(order)
        transition_order_status(order, "ACCEPTED")
        transition_order_status(order, "PREPARING")
        transition_order_status(order, "READY")
        transition_order_status(order, "SERVED")
        return order

    def test_scenario_a_qr_pay_now_anonymous(self):
        """A) QR customer chooses PAY NOW: stays anonymous, never requires login, order enters kitchen as NEW."""
        self.seed_cart(items=[(self.item_a, 2)])
        checkout_url = reverse("checkout", args=[self.restaurant.pk, self.table.pk])
        response = self.client.post(
            checkout_url,
            {
                "order_type": "DINE_IN",
                "payment_timing": "PAY_NOW",
                "payment_method": "CARD",
                "payment_reference": "TXN-ANON-101",
            },
        )
        self.assertRedirects(response, reverse("order_success", args=[Order.objects.last().id]))
        order = Order.objects.last()
        self.assertEqual(order.payment_status, "PAID")
        self.assertEqual(order.status, "NEW")
        self.assertEqual(order.payment_method, "CARD")
        self.assertEqual(order.payment_reference, "TXN-ANON-101")
        # Customer can access order tracking and receipt anonymously without being redirected to login
        track_resp = self.client.get(reverse("order_success", args=[order.id]))
        self.assertEqual(track_resp.status_code, 200)
        receipt_resp = self.client.get(reverse("order_receipt", args=[order.id]))
        self.assertEqual(receipt_resp.status_code, 200)

    def test_scenario_b_qr_pay_later_anonymous(self):
        """B) QR customer chooses PAY LATER: stays anonymous, never requires login, enters kitchen unpaid."""
        self.seed_cart(items=[(self.item_a, 1)])
        checkout_url = reverse("checkout", args=[self.restaurant.pk, self.table.pk])
        response = self.client.post(
            checkout_url,
            {
                "order_type": "DINE_IN",
                "payment_timing": "PAY_LATER",
            },
        )
        order = Order.objects.last()
        self.assertRedirects(response, reverse("order_success", args=[order.id]))
        self.assertEqual(order.payment_status, "UNPAID")
        self.assertEqual(order.status, "NEW")
        track_resp = self.client.get(reverse("order_success", args=[order.id]))
        self.assertEqual(track_resp.status_code, 200)

    def test_scenario_c_same_table_multiple_orders_same_session_and_combined_bill(self):
        """C) Same table places multiple orders in same sitting: reuses TableSession, combined running bill."""
        # Order 1: Drinks / Lassi
        self.seed_cart(items=[(self.item_a, 1)])
        self.client.post(
            reverse("checkout", args=[self.restaurant.pk, self.table.pk]),
            {"order_type": "DINE_IN", "payment_timing": "PAY_LATER"},
        )
        order1 = Order.objects.last()
        session1 = order1.table_session
        self.assertIsNotNone(session1)
        self.assertTrue(session1.is_open)

        # Customer re-scans table and places Order 2: Food
        self.seed_cart(items=[(self.item_b, 2)])
        checkout_url = reverse("checkout", args=[self.restaurant.pk, self.table.pk])
        get_checkout = self.client.get(checkout_url)
        self.assertContains(get_checkout, f"Order #{order1.id}")
        self.assertContains(get_checkout, "Combined Running Table Bill")

        self.client.post(
            checkout_url,
            {"order_type": "DINE_IN", "payment_timing": "PAY_LATER"},
        )
        order2 = Order.objects.last()
        self.assertNotEqual(order1.id, order2.id)
        # Reuses the exact same TableSession!
        self.assertEqual(order2.table_session_id, session1.id)
        self.assertEqual(TableSession.objects.filter(table=self.table, status=TableSession.STATUS_OPEN).count(), 1)

        # Both exist as separate kitchen orders
        self.assertEqual(order1.status, "NEW")
        self.assertEqual(order2.status, "NEW")

        # Summary combines both orders
        summary = _table_service_summary(order2)
        self.assertEqual(summary["orders_count"], 2)
        self.assertEqual(summary["total_amount"], order1.total_amount + order2.total_amount)
        self.assertEqual(summary["unpaid_amount"], order1.total_amount + order2.total_amount)

    def test_scenario_d_e_f_full_flow_payment_completion_table_freed_and_feedback(self):
        """D, E, F) Orders served, owner collects payment on Orders page, orders complete, table freed, receipt & feedback verified."""
        from orders.services import transition_order_status
        # Place 2 orders at same table
        self.seed_cart(items=[(self.item_a, 1)])
        self.client.post(
            reverse("checkout", args=[self.restaurant.pk, self.table.pk]),
            {"order_type": "DINE_IN", "payment_timing": "PAY_LATER"},
        )
        order1 = Order.objects.last()

        self.seed_cart(items=[(self.item_b, 1)])
        self.client.post(
            reverse("checkout", args=[self.restaurant.pk, self.table.pk]),
            {"order_type": "DINE_IN", "payment_timing": "PAY_LATER"},
        )
        order2 = Order.objects.last()
        session = order1.table_session

        # Kitchen advances: NEW -> ACCEPTED -> PREPARING -> READY
        for ord_item in (order1, order2):
            transition_order_status(ord_item, "ACCEPTED")
            transition_order_status(ord_item, "PREPARING")
            transition_order_status(ord_item, "READY")
            # Waiter advances: READY -> SERVED
            transition_order_status(ord_item, "SERVED")

        # Cannot mark COMPLETED while unpaid
        with self.assertRaises(ValidationError):
            transition_order_status(order1, "COMPLETED")

        # Owner checks Orders page
        staff_client = Client()
        staff_client.force_login(self.staff_user)
        orders_page = staff_client.get(reverse("orders_list"))
        self.assertEqual(orders_page.status_code, 200)
        self.assertContains(orders_page, "Payment Due / Collect Payment")
        self.assertContains(orders_page, f"Table {self.table.table_number}")
        total_due = order1.total_amount + order2.total_amount
        self.assertContains(orders_page, f"{total_due:.2f}")

        # Owner collects payment for entire table service
        pay_url = reverse("bill_preview", args=[order1.id])
        pay_resp = staff_client.post(
            pay_url,
            {
                "settle_scope": "TABLE",
                "payment_method": "MOBILE_BANKING",
                "payment_reference": "BKASH-9988",
            },
        )
        self.assertIn(pay_resp.status_code, [301, 302])

        # Both orders are now PAID and COMPLETED
        order1.refresh_from_db()
        order2.refresh_from_db()
        self.assertEqual(order1.payment_status, "PAID")
        self.assertEqual(order2.payment_status, "PAID")
        self.assertEqual(order1.status, "COMPLETED")
        self.assertEqual(order2.status, "COMPLETED")

        # Table session is closed and clean_needed is True
        session.refresh_from_db()
        self.assertTrue(session.is_closed)
        self.assertTrue(session.clean_needed)

        # Table is immediately AVAILABLE
        self.table.refresh_from_db()
        self.assertEqual(self.table.status, Table.STATUS_AVAILABLE)

        # Receipt displays combined service bill
        receipt_resp = staff_client.get(reverse("order_receipt", args=[order1.id]))
        self.assertContains(receipt_resp, "Combined Table Service Bill")
        self.assertContains(receipt_resp, f"Order #{order1.id}")
        self.assertContains(receipt_resp, f"Order #{order2.id}")

        # Receipt for order2 also generates individual feedback for order2
        receipt_resp2 = staff_client.get(reverse("order_receipt", args=[order2.id]))
        self.assertEqual(receipt_resp2.status_code, 200)

        # Feedback QR works for each individual order after completion
        from guests.models import OrderFeedback
        feedback1 = OrderFeedback.objects.get(order=order1)
        feedback2 = OrderFeedback.objects.get(order=order2)
        self.assertNotEqual(feedback1.token, feedback2.token)
        fb_resp1 = self.client.get(reverse("guests:feedback", args=[feedback1.token]))
        self.assertEqual(fb_resp1.status_code, 200)
        fb_resp2 = self.client.get(reverse("guests:feedback", args=[feedback2.token]))
        self.assertEqual(fb_resp2.status_code, 200)

    def test_regression_repeated_payment_posts_idempotency(self):
        """Repeated payment POSTs must not double-charge or create duplicate transactions."""
        order = self._create_served_order(quantity=2)
        url = reverse("bill_preview", args=[order.pk])
        client = Client()
        client.force_login(self.staff_user)

        # First payment
        resp1 = client.post(url, {"settle_scope": "TABLE", "payment_method": "CASH"})
        self.assertIn(resp1.status_code, [301, 302])
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "PAID")
        self.assertEqual(order.transactions.count(), 1)

        # Second payment attempt (repeated POST)
        resp2 = client.post(url, {"settle_scope": "TABLE", "payment_method": "CASH"})
        self.assertIn(resp2.status_code, [301, 302])
        order.refresh_from_db()
        self.assertEqual(order.transactions.count(), 1)

    def test_regression_mixed_paid_and_unpaid_orders_table_settlement(self):
        """Table settlement charges only unpaid orders and completes all served orders."""
        from orders.services import transition_order_status
        # Order 1: PAY NOW (paid immediately)
        self.seed_cart(items=[(self.item_a, 1)])
        self.client.post(
            reverse("checkout", args=[self.restaurant.pk, self.table.pk]),
            {"order_type": "DINE_IN", "payment_timing": "PAY_NOW", "payment_method": "CARD"},
        )
        order1 = Order.objects.last()
        self.assertEqual(order1.payment_status, "PAID")

        # Order 2: PAY LATER (unpaid)
        self.seed_cart(items=[(self.item_b, 1)])
        self.client.post(
            reverse("checkout", args=[self.restaurant.pk, self.table.pk]),
            {"order_type": "DINE_IN", "payment_timing": "PAY_LATER"},
        )
        order2 = Order.objects.last()
        self.assertEqual(order2.payment_status, "UNPAID")

        # Both served
        for ord_item in (order1, order2):
            transition_order_status(ord_item, "ACCEPTED")
            transition_order_status(ord_item, "PREPARING")
            transition_order_status(ord_item, "READY")
            transition_order_status(ord_item, "SERVED")

        # Settle table
        staff_client = Client()
        staff_client.force_login(self.staff_user)
        staff_client.post(
            reverse("bill_preview", args=[order2.pk]),
            {"settle_scope": "TABLE", "payment_method": "CASH"},
        )

        order1.refresh_from_db()
        order2.refresh_from_db()
        self.assertEqual(order1.payment_status, "PAID")
        self.assertEqual(order2.payment_status, "PAID")
        # Both become COMPLETED
        self.assertEqual(order1.status, "COMPLETED")
        self.assertEqual(order2.status, "COMPLETED")
        # Order 1 was not recharged
        self.assertEqual(order1.transactions.count(), 1)
        self.assertEqual(order2.transactions.count(), 1)

    def test_regression_cancelled_order_excluded_from_table_settlement(self):
        """Cancelled orders must not be charged during table settlement."""
        from orders.services import archive_stale_order, transition_order_status
        self.seed_cart(items=[(self.item_a, 1)])
        self.client.post(
            reverse("checkout", args=[self.restaurant.pk, self.table.pk]),
            {"order_type": "DINE_IN", "payment_timing": "PAY_LATER"},
        )
        order1 = Order.objects.last()
        archive_stale_order(order1)
        self.assertEqual(order1.status, Order.STATUS_CANCELLED)

        # Order 2 is valid and served
        self.seed_cart(items=[(self.item_b, 1)])
        self.client.post(
            reverse("checkout", args=[self.restaurant.pk, self.table.pk]),
            {"order_type": "DINE_IN", "payment_timing": "PAY_LATER"},
        )
        order2 = Order.objects.last()
        transition_order_status(order2, "ACCEPTED")
        transition_order_status(order2, "PREPARING")
        transition_order_status(order2, "READY")
        transition_order_status(order2, "SERVED")

        summary = _table_service_summary(order2)
        self.assertEqual(summary["total_amount"], order2.total_amount)
        self.assertEqual(summary["orders_count"], 1)

        staff_client = Client()
        staff_client.force_login(self.staff_user)
        staff_client.post(
            reverse("bill_preview", args=[order2.pk]),
            {"settle_scope": "TABLE", "payment_method": "CASH"},
        )
        order1.refresh_from_db()
        order2.refresh_from_db()
        self.assertEqual(order1.payment_status, "UNPAID")
        self.assertEqual(order1.transactions.count(), 0)
        self.assertEqual(order2.payment_status, "PAID")
        self.assertEqual(order2.status, "COMPLETED")
