import json
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from finance.reports_services import get_item_sales_report_data, get_sales_report_data
from inventory.models import (
    Ingredient,
    IngredientCategory,
    Recipe,
    RecipeIngredient,
)
from menu.models import AddonGroup, AddonOption, Category, MenuItem
from orders.coupon_services import (
    apply_coupon_usage_atomic,
    calculate_order_pricing,
    find_best_automatic_offer,
    validate_and_calculate_coupon,
)
from orders.models import Coupon, Order, OrderItem, OrderItemAddon, PaymentTransaction
from restaurant.models import Restaurant, Table
from users.models import User


class CouponSystemTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.restaurant = Restaurant.objects.create(name="Grand Bistro", address="Dhaka")
        cls.other_restaurant = Restaurant.objects.create(name="Other Cafe", address="Chittagong")

        cls.owner = User.objects.create_user(
            username="owner_user",
            email="owner@test.com",
            password="password123",
            role="owner",
            restaurant=cls.restaurant,
        )

        cls.table = Table.objects.create(
            restaurant=cls.restaurant,
            table_number=1,
            qr_code="test-fixtures/table.png",
        )

        cls.cat_burgers = Category.objects.create(restaurant=cls.restaurant, name="Burgers")
        cls.cat_drinks = Category.objects.create(restaurant=cls.restaurant, name="Drinks")

        cls.burger = MenuItem.objects.create(
            category=cls.cat_burgers,
            name="Beef Burger",
            price=Decimal("200.00"),
            is_available=True,
        )
        cls.coke = MenuItem.objects.create(
            category=cls.cat_drinks,
            name="Cold Coke",
            price=Decimal("50.00"),
            is_available=True,
        )

        cls.ing_cat = IngredientCategory.objects.create(restaurant=cls.restaurant, name="Pantry")
        cls.beef = Ingredient.objects.create(
            restaurant=cls.restaurant,
            category=cls.ing_cat,
            name="Beef",
            sku="BEEF01",
            base_unit=Ingredient.BaseUnit.GRAM,
            pack_size=1000,
            current_stock=10000,
        )
        cls.syrup = Ingredient.objects.create(
            restaurant=cls.restaurant,
            category=cls.ing_cat,
            name="Syrup",
            sku="SYR01",
            base_unit=Ingredient.BaseUnit.MILLILITRE,
            pack_size=1000,
            current_stock=10000,
        )
        recipe_burger = Recipe.objects.create(menu_item=cls.burger, yield_quantity=1)
        RecipeIngredient.objects.create(recipe=recipe_burger, ingredient=cls.beef, quantity=150)
        recipe_coke = Recipe.objects.create(menu_item=cls.coke, yield_quantity=1)
        RecipeIngredient.objects.create(recipe=recipe_coke, ingredient=cls.syrup, quantity=50)

    def setUp(self):
        self.client = Client()

    def test_percentage_discount_with_and_without_cap(self):
        # 10% coupon without cap
        coupon_nocap = Coupon.objects.create(
            restaurant=self.restaurant,
            name="10% Off",
            code="TEN",
            discount_type=Coupon.DISCOUNT_TYPE_PERCENTAGE,
            discount_value=Decimal("10.00"),
            applicability=Coupon.APPLICABILITY_WHOLE_ORDER,
        )

        items_data = [
            {"menu_item": self.burger, "quantity": 2, "subtotal": Decimal("400.00")},
            {"menu_item": self.coke, "quantity": 2, "subtotal": Decimal("100.00")},
        ]
        res = validate_and_calculate_coupon(coupon_nocap, items_data)
        # Total = 500, 10% = 50.00
        self.assertEqual(res["discount_amount"], Decimal("50.00"))
        # Check proportional allocation
        self.assertEqual(items_data[0]["discount_amount"], Decimal("40.00"))
        self.assertEqual(items_data[1]["discount_amount"], Decimal("10.00"))

        # 20% coupon capped at 30.00
        coupon_capped = Coupon.objects.create(
            restaurant=self.restaurant,
            name="20% Max 30",
            code="TWENTYCAP",
            discount_type=Coupon.DISCOUNT_TYPE_PERCENTAGE,
            discount_value=Decimal("20.00"),
            max_discount_amount=Decimal("30.00"),
            applicability=Coupon.APPLICABILITY_WHOLE_ORDER,
        )
        # 20% of 500 = 100, but cap is 30
        res_cap = validate_and_calculate_coupon(coupon_capped, items_data)
        self.assertEqual(res_cap["discount_amount"], Decimal("30.00"))

    def test_fixed_discount_capped_at_subtotal(self):
        coupon_fixed = Coupon.objects.create(
            restaurant=self.restaurant,
            name="50 Off",
            code="FLAT50",
            discount_type=Coupon.DISCOUNT_TYPE_FIXED,
            discount_value=Decimal("50.00"),
            applicability=Coupon.APPLICABILITY_WHOLE_ORDER,
        )
        items_data = [
            {"menu_item": self.coke, "quantity": 1, "subtotal": Decimal("50.00")},
        ]
        res = validate_and_calculate_coupon(coupon_fixed, items_data)
        self.assertEqual(res["discount_amount"], Decimal("50.00"))

        # If discount exceeds subtotal, capped at subtotal
        coupon_huge = Coupon.objects.create(
            restaurant=self.restaurant,
            name="100 Off",
            code="FLAT100",
            discount_type=Coupon.DISCOUNT_TYPE_FIXED,
            discount_value=Decimal("100.00"),
            applicability=Coupon.APPLICABILITY_WHOLE_ORDER,
        )
        res_huge = validate_and_calculate_coupon(coupon_huge, items_data)
        self.assertEqual(res_huge["discount_amount"], Decimal("50.00"))

    def test_code_vs_automatic_offers_and_best_selection(self):
        # 1. Automatic offer 1: 5% off
        auto_5 = Coupon.objects.create(
            restaurant=self.restaurant,
            name="Auto 5%",
            is_automatic=True,
            discount_type=Coupon.DISCOUNT_TYPE_PERCENTAGE,
            discount_value=Decimal("5.00"),
            applicability=Coupon.APPLICABILITY_WHOLE_ORDER,
        )
        # 2. Automatic offer 2: 15% off
        auto_15 = Coupon.objects.create(
            restaurant=self.restaurant,
            name="Auto 15%",
            is_automatic=True,
            discount_type=Coupon.DISCOUNT_TYPE_PERCENTAGE,
            discount_value=Decimal("15.00"),
            applicability=Coupon.APPLICABILITY_WHOLE_ORDER,
        )
        # 3. Manual code: 25% off
        code_25 = Coupon.objects.create(
            restaurant=self.restaurant,
            name="VIP 25%",
            code="VIP25",
            is_automatic=False,
            discount_type=Coupon.DISCOUNT_TYPE_PERCENTAGE,
            discount_value=Decimal("25.00"),
            applicability=Coupon.APPLICABILITY_WHOLE_ORDER,
        )

        items_data = [
            {"menu_item": self.burger, "quantity": 1, "subtotal": Decimal("200.00")},
        ]

        # No code: chooses best automatic offer (15% = 30.00)
        p_auto = calculate_order_pricing(self.restaurant, items_data)
        self.assertEqual(p_auto["applied_coupon"].id, auto_15.id)
        self.assertEqual(p_auto["discount_amount"], Decimal("30.00"))

        # Providing manual code: overrides automatic offers and applies 25% (= 50.00)
        p_code = calculate_order_pricing(self.restaurant, items_data, coupon_code="vip25")
        self.assertEqual(p_code["applied_coupon"].id, code_25.id)
        self.assertEqual(p_code["discount_amount"], Decimal("50.00"))

    def test_validity_dates_min_order_and_usage_limits(self):
        now = timezone.now()

        # Future coupon
        future_coupon = Coupon.objects.create(
            restaurant=self.restaurant,
            name="Future",
            code="FUTURE",
            discount_type=Coupon.DISCOUNT_TYPE_FIXED,
            discount_value=Decimal("20.00"),
            start_datetime=now + timezone.timedelta(days=1),
        )
        items = [{"menu_item": self.burger, "quantity": 1, "subtotal": Decimal("200.00")}]
        with self.assertRaises(ValidationError) as ctx:
            validate_and_calculate_coupon(future_coupon, items)
        self.assertIn("not valid yet", str(ctx.exception))

        # Expired coupon
        expired_coupon = Coupon.objects.create(
            restaurant=self.restaurant,
            name="Expired",
            code="EXPIRED",
            discount_type=Coupon.DISCOUNT_TYPE_FIXED,
            discount_value=Decimal("20.00"),
            end_datetime=now - timezone.timedelta(hours=1),
        )
        with self.assertRaises(ValidationError) as ctx:
            validate_and_calculate_coupon(expired_coupon, items)
        self.assertIn("expired", str(ctx.exception))

        # Minimum order amount
        min_coupon = Coupon.objects.create(
            restaurant=self.restaurant,
            name="Min 300",
            code="MIN300",
            discount_type=Coupon.DISCOUNT_TYPE_FIXED,
            discount_value=Decimal("50.00"),
            min_order_amount=Decimal("300.00"),
        )
        with self.assertRaises(ValidationError) as ctx:
            validate_and_calculate_coupon(min_coupon, items)  # subtotal is 200
        self.assertIn("Minimum order amount", str(ctx.exception))

        # Usage limit reached
        limit_coupon = Coupon.objects.create(
            restaurant=self.restaurant,
            name="Limit 1",
            code="LIMIT1",
            discount_type=Coupon.DISCOUNT_TYPE_FIXED,
            discount_value=Decimal("10.00"),
            usage_limit=1,
            times_used=1,
        )
        with self.assertRaises(ValidationError) as ctx:
            validate_and_calculate_coupon(limit_coupon, items)
        self.assertIn("maximum usage limit", str(ctx.exception))

    def test_applicability_scoping_category_and_menu_item(self):
        # Coupon applies ONLY to Burgers category
        cat_coupon = Coupon.objects.create(
            restaurant=self.restaurant,
            name="Burgers 50% Off",
            code="BURGER50",
            discount_type=Coupon.DISCOUNT_TYPE_PERCENTAGE,
            discount_value=Decimal("50.00"),
            applicability=Coupon.APPLICABILITY_CATEGORY,
        )
        cat_coupon.applicable_categories.add(self.cat_burgers)

        # Cart has Burger (200.00) and Coke (50.00)
        items_data = [
            {"menu_item": self.burger, "quantity": 1, "subtotal": Decimal("200.00")},
            {"menu_item": self.coke, "quantity": 1, "subtotal": Decimal("50.00")},
        ]
        res = validate_and_calculate_coupon(cat_coupon, items_data)
        # 50% of Burger only (200 * 50% = 100.00)
        self.assertEqual(res["discount_amount"], Decimal("100.00"))
        self.assertEqual(items_data[0]["discount_amount"], Decimal("100.00"))
        self.assertEqual(items_data[1]["discount_amount"], Decimal("0.00"))

        # Coupon applies ONLY to Coke menu item
        item_coupon = Coupon.objects.create(
            restaurant=self.restaurant,
            name="Coke ৳20 Off",
            code="COKE20",
            discount_type=Coupon.DISCOUNT_TYPE_FIXED,
            discount_value=Decimal("20.00"),
            applicability=Coupon.APPLICABILITY_MENU_ITEM,
        )
        item_coupon.applicable_items.add(self.coke)
        res_item = validate_and_calculate_coupon(item_coupon, items_data)
        self.assertEqual(res_item["discount_amount"], Decimal("20.00"))
        self.assertEqual(items_data[0]["discount_amount"], Decimal("0.00"))
        self.assertEqual(items_data[1]["discount_amount"], Decimal("20.00"))

    def test_immutable_order_snapshots_when_coupon_edited_or_deleted(self):
        coupon = Coupon.objects.create(
            restaurant=self.restaurant,
            name="Original 20%",
            code="ORIG20",
            discount_type=Coupon.DISCOUNT_TYPE_PERCENTAGE,
            discount_value=Decimal("20.00"),
        )

        items_data = [
            {"menu_item": self.burger, "quantity": 1, "subtotal": Decimal("200.00")},
        ]
        pricing = calculate_order_pricing(self.restaurant, items_data, coupon_code="ORIG20")

        order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_type="DINE_IN",
            subtotal=pricing["subtotal"],
            discount_amount=pricing["discount_amount"],
            service_charge=pricing["service_charge"],
            vat_amount=pricing["vat_amount"],
            total_amount=pricing["total_amount"],
            applied_coupon=pricing["applied_coupon"],
            coupon_code_snapshot=pricing["coupon_code_snapshot"],
            coupon_name_snapshot=pricing["coupon_name_snapshot"],
            discount_type_snapshot=pricing["discount_type_snapshot"],
            discount_rate_snapshot=pricing["discount_rate_snapshot"],
            payment_status="PAID",
        )
        item = OrderItem.objects.create(
            order=order,
            menu_item=self.burger,
            quantity=1,
            price=self.burger.price,
            discount_amount=pricing["items_data"][0]["discount_amount"],
        )

        self.assertEqual(order.discount_amount, Decimal("40.00"))
        self.assertEqual(order.total_amount, Decimal("160.00"))
        self.assertEqual(order.coupon_code_snapshot, "ORIG20")
        self.assertEqual(item.discount_amount, Decimal("40.00"))
        self.assertEqual(item.net_subtotal, Decimal("160.00"))

        # Edit coupon to 50%
        coupon.discount_value = Decimal("50.00")
        coupon.name = "Edited 50%"
        coupon.save()

        # Historical order must remain unchanged
        order.refresh_from_db()
        self.assertEqual(order.discount_amount, Decimal("40.00"))
        self.assertEqual(order.total_amount, Decimal("160.00"))
        self.assertEqual(order.coupon_code_snapshot, "ORIG20")
        self.assertEqual(order.coupon_name_snapshot, "Original 20%")

        # Delete coupon completely
        coupon.delete()
        order.refresh_from_db()
        self.assertIsNone(order.applied_coupon)
        self.assertEqual(order.discount_amount, Decimal("40.00"))
        self.assertEqual(order.coupon_code_snapshot, "ORIG20")
        self.assertEqual(order.coupon_name_snapshot, "Original 20%")

    def test_pos_coupon_and_manual_discount_mutual_exclusivity(self):
        self.client.force_login(self.owner)

        Coupon.objects.create(
            restaurant=self.restaurant,
            name="10 Off",
            code="TENOFF",
            discount_type=Coupon.DISCOUNT_TYPE_FIXED,
            discount_value=Decimal("10.00"),
        )

        # Attempt to submit both coupon_code and manual discount_amount
        payload_stacked = {
            "order_type": "TAKEAWAY",
            "items": [{"id": self.burger.id, "quantity": 1}],
            "coupon_code": "TENOFF",
            "discount_amount": 15,
        }
        resp = self.client.post(
            reverse("create_pos_order"),
            data=json.dumps(payload_stacked),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertIn("Cannot combine", data["message"])

        # Submit only coupon
        payload_coupon = {
            "order_type": "TAKEAWAY",
            "items": [{"id": self.burger.id, "quantity": 1}],
            "coupon_code": "TENOFF",
            "discount_amount": 0,
        }
        resp_c = self.client.post(
            reverse("create_pos_order"),
            data=json.dumps(payload_coupon),
            content_type="application/json",
        )
        self.assertEqual(resp_c.status_code, 200)
        c_order = Order.objects.get(id=resp_c.json()["order_id"])
        self.assertEqual(c_order.discount_amount, Decimal("10.00"))
        self.assertEqual(c_order.coupon_code_snapshot, "TENOFF")

        # Submit only manual discount
        payload_manual = {
            "order_type": "TAKEAWAY",
            "items": [{"id": self.burger.id, "quantity": 1}],
            "coupon_code": "",
            "discount_amount": 25,
        }
        resp_m = self.client.post(
            reverse("create_pos_order"),
            data=json.dumps(payload_manual),
            content_type="application/json",
        )
        self.assertEqual(resp_m.status_code, 200)
        m_order = Order.objects.get(id=resp_m.json()["order_id"])
        self.assertEqual(m_order.discount_amount, Decimal("25.00"))
        self.assertEqual(m_order.coupon_code_snapshot, "")

    def test_atomic_usage_limit_enforcement(self):
        coupon = Coupon.objects.create(
            restaurant=self.restaurant,
            name="Single Use",
            code="ONCE",
            discount_type=Coupon.DISCOUNT_TYPE_FIXED,
            discount_value=Decimal("50.00"),
            usage_limit=1,
            times_used=0,
        )

        self.client.force_login(self.owner)

        # First order uses it -> succeeds
        resp1 = self.client.post(
            reverse("create_pos_order"),
            data=json.dumps({
                "order_type": "TAKEAWAY",
                "items": [{"id": self.burger.id, "quantity": 1}],
                "coupon_code": "ONCE",
            }),
            content_type="application/json",
        )
        self.assertEqual(resp1.status_code, 200)
        coupon.refresh_from_db()
        self.assertEqual(coupon.times_used, 1)

        # Second order tries to use it -> rejected
        resp2 = self.client.post(
            reverse("create_pos_order"),
            data=json.dumps({
                "order_type": "TAKEAWAY",
                "items": [{"id": self.burger.id, "quantity": 1}],
                "coupon_code": "ONCE",
            }),
            content_type="application/json",
        )
        self.assertEqual(resp2.status_code, 400)
        self.assertIn("usage limit", resp2.json()["message"])

    def test_qr_and_pos_pricing_parity(self):
        Coupon.objects.create(
            restaurant=self.restaurant,
            name="Save 20%",
            code="SAVE20",
            discount_type=Coupon.DISCOUNT_TYPE_PERCENTAGE,
            discount_value=Decimal("20.00"),
        )

        # 1. POS order creation with SAVE20
        self.client.force_login(self.owner)
        pos_resp = self.client.post(
            reverse("create_pos_order"),
            data=json.dumps({
                "order_type": "DINE_IN",
                "table_id": self.table.id,
                "items": [{"id": self.burger.id, "quantity": 2}],
                "coupon_code": "SAVE20",
            }),
            content_type="application/json",
        )
        self.assertEqual(pos_resp.status_code, 200)
        pos_order = Order.objects.get(id=pos_resp.json()["order_id"])

        # 2. QR order creation with SAVE20
        session = self.client.session
        from menu.views import get_cart_key
        cart_key = get_cart_key(self.restaurant.id, self.table.id)
        session[cart_key] = {
            str(self.burger.id): {
                "name": self.burger.name,
                "quantity": 2,
                "price": str(self.burger.price),
                "subtotal": "400.00",
                "addon_ids": [],
            }
        }
        session.save()

        qr_resp = self.client.post(
            reverse("checkout", args=[self.restaurant.id, self.table.id]),
            data={
                "order_type": "DINE_IN",
                "payment_timing": "PAY_LATER",
                "coupon_code": "SAVE20",
            },
        )
        self.assertEqual(qr_resp.status_code, 302)
        qr_order = Order.objects.latest("id")

        # Subtotal, discount and total must be IDENTICAL
        self.assertEqual(pos_order.subtotal, qr_order.subtotal)
        self.assertEqual(pos_order.discount_amount, qr_order.discount_amount)
        self.assertEqual(pos_order.total_amount, qr_order.total_amount)
        self.assertEqual(qr_order.subtotal, Decimal("400.00"))
        self.assertEqual(qr_order.discount_amount, Decimal("80.00"))
        self.assertEqual(qr_order.total_amount, Decimal("320.00"))

    def test_financial_reporting_and_item_sales_with_discounts(self):
        # Order with 50 discount
        order = Order.objects.create(
            restaurant=self.restaurant,
            order_type="TAKEAWAY",
            status=Order.STATUS_COMPLETED,
            subtotal=Decimal("200.00"),
            discount_amount=Decimal("50.00"),
            service_charge=Decimal("0.00"),
            vat_amount=Decimal("0.00"),
            total_amount=Decimal("150.00"),
            payment_status="PAID",
        )
        # OrderItem has discount_amount = 50.00
        OrderItem.objects.create(
            order=order,
            menu_item=self.burger,
            quantity=1,
            price=Decimal("200.00"),
            discount_amount=Decimal("50.00"),
        )
        now = timezone.now()
        PaymentTransaction.objects.create(
            restaurant=self.restaurant,
            order=order,
            transaction_type=PaymentTransaction.TYPE_PAYMENT,
            amount=Decimal("150.00"),
            payment_method="CASH",
            transaction_at=now,
        )

        start = now - timezone.timedelta(days=1)
        end = now + timezone.timedelta(days=1)

        # Sales report
        sales = get_sales_report_data(self.restaurant, start, end)
        self.assertEqual(sales["gross_collections"], Decimal("150.00"))
        self.assertEqual(sales["net_sales"], Decimal("150.00"))

        # Item sales report uses item.net_subtotal = 200 - 50 = 150
        item_sales = get_item_sales_report_data(self.restaurant, start, end)
        burger_row = next(r for r in item_sales["items"] if r["name"] == self.burger.name)
        self.assertEqual(burger_row["sales_amount"], Decimal("150.00"))

    def test_owner_crud_views(self):
        self.client.force_login(self.owner)

        # Create coupon
        resp_c = self.client.post(
            reverse("coupon_create"),
            data={
                "name": "Weekend Special",
                "code": "WEEKEND",
                "is_automatic": "on",
                "discount_type": "PERCENTAGE",
                "discount_value": "15.00",
                "min_order_amount": "100.00",
                "is_active": "on",
                "applicability": "WHOLE_ORDER",
            },
        )
        self.assertEqual(resp_c.status_code, 302)
        coupon = Coupon.objects.get(code="WEEKEND")
        self.assertEqual(coupon.discount_value, Decimal("15.00"))
        self.assertTrue(coupon.is_automatic)

        # List view
        resp_l = self.client.get(reverse("coupon_list"))
        self.assertEqual(resp_l.status_code, 200)
        self.assertContains(resp_l, "Weekend Special")
        self.assertContains(resp_l, "WEEKEND")

        # Edit coupon
        resp_e = self.client.post(
            reverse("coupon_edit", args=[coupon.id]),
            data={
                "name": "Weekend Super Special",
                "code": "WEEKEND",
                "is_automatic": "",
                "discount_type": "FIXED",
                "discount_value": "35.00",
                "min_order_amount": "150.00",
                "is_active": "on",
                "applicability": "WHOLE_ORDER",
            },
        )
        self.assertEqual(resp_e.status_code, 302)
        coupon.refresh_from_db()
        self.assertEqual(coupon.name, "Weekend Super Special")
        self.assertEqual(coupon.discount_type, "FIXED")
        self.assertEqual(coupon.discount_value, Decimal("35.00"))
        self.assertFalse(coupon.is_automatic)

        # Toggle coupon
        self.client.post(reverse("coupon_toggle", args=[coupon.id]))
        coupon.refresh_from_db()
        self.assertFalse(coupon.is_active)

        # Delete coupon
        resp_d = self.client.post(reverse("coupon_delete", args=[coupon.id]))
        self.assertEqual(resp_d.status_code, 302)
        self.assertFalse(Coupon.objects.filter(id=coupon.id).exists())
