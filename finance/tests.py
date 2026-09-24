from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.utils import timezone

from finance.date_utils import parse_date_range
from finance.models import Expense, ExpenseCategory
from finance.money_utils import format_money, normalize_zero_money
from finance.reports_services import (
    get_item_sales_report_data,
    get_profit_and_loss_data,
    get_purchase_report_data,
    get_sales_report_data,
    get_stock_report_data,
)
from inventory.models import (
    Ingredient,
    IngredientCategory,
    PurchaseOrder,
    PurchaseOrderItem,
    Recipe,
    RecipeIngredient,
    StockTransaction,
    Supplier,
    WasteRecord,
)
from menu.models import Category, MenuItem
from orders.models import Order, OrderItem, PaymentTransaction
from orders.services import record_order_payment, record_order_refund
from restaurant.models import Branch, Restaurant, Table
from staff.models import EmployeeProfile, PayrollRecord, SalaryAdvance

User = get_user_model()


class FinanceAndReportingTests(TestCase):
    def setUp(self):
        self.tz = timezone.get_current_timezone()
        self.today = timezone.localdate()

        # Create two restaurants for multi-tenancy tests
        self.restaurant_a = Restaurant.objects.create(
            name="Test Bistro A", address="100 Main St"
        )
        self.restaurant_b = Restaurant.objects.create(
            name="Test Bistro B", address="200 West St"
        )

        for restaurant in (self.restaurant_a, self.restaurant_b):
            Branch.objects.create(restaurant=restaurant, name="Main", code="MAIN", is_main=True)

        self.user_a = User.objects.create_user(
            username="owner_a",
            email="owner_a@test.com",
            password="pass",
            role="owner",
            restaurant=self.restaurant_a,
        )

        self.user_b = User.objects.create_user(
            username="owner_b",
            email="owner_b@test.com",
            password="pass",
            role="owner",
            restaurant=self.restaurant_b,
        )

        self.table_a = Table.objects.create(
            restaurant=self.restaurant_a, table_number=1
        )
        self.cat_a = Category.objects.create(
            restaurant=self.restaurant_a, name="Mains"
        )
        self.menu_item_a = MenuItem.objects.create(
            category=self.cat_a,
            name="Steak Special",
            price=Decimal("50.00"),
        )

        # Inventory setup for restaurant A
        self.ing_cat_a = IngredientCategory.objects.create(
            restaurant=self.restaurant_a, name="Meat"
        )
        self.meat = Ingredient.objects.create(
            restaurant=self.restaurant_a,
            category=self.ing_cat_a,
            name="Prime Beef",
            sku="BEEF-001",
            base_unit=Ingredient.BaseUnit.GRAM,
            pack_size=Decimal("1000.000"),
            current_pack_price=Decimal("20.00"),  # $0.02 per gram
            current_stock=Decimal("5000.000"),
        )

        # Recipe for steak (250g meat)
        self.recipe = Recipe.objects.create(
            menu_item=self.menu_item_a,
            yield_quantity=Decimal("1.00"),
        )
        RecipeIngredient.objects.create(
            recipe=self.recipe,
            ingredient=self.meat,
            quantity=Decimal("250.000"),
        )

        self.client = Client()

    def _get_day_boundaries(self, target_date):
        start_naive = datetime.combine(target_date, time.min)
        end_naive = datetime.combine(target_date, time.max)
        return timezone.make_aware(start_naive, self.tz), timezone.make_aware(end_naive, self.tz)

    def test_unpaid_order_not_counted_as_revenue(self):
        """Unpaid orders must never appear as revenue in Sales Report or P&L."""
        order = Order.objects.create(
            restaurant=self.restaurant_a,
            table=self.table_a,
            order_type="DINE_IN",
            status="NEW",
            subtotal=Decimal("100.00"),
            total_amount=Decimal("100.00"),
            payment_status="UNPAID",
        )

        start_dt, end_dt = self._get_day_boundaries(self.today)
        sales = get_sales_report_data(self.restaurant_a, start_dt, end_dt)

        self.assertEqual(sales["gross_collections"], Decimal("0.00"))
        self.assertEqual(sales["net_collections"], Decimal("0.00"))
        self.assertEqual(sales["net_sales"], Decimal("0.00"))
        self.assertEqual(sales["order_count"], 0)

    def test_payment_recorded_once_and_idempotent(self):
        """PaymentTransaction creation must be idempotent and not duplicate revenue on retries."""
        order = Order.objects.create(
            restaurant=self.restaurant_a,
            table=self.table_a,
            order_type="DINE_IN",
            status="SERVED",
            subtotal=Decimal("80.00"),
            total_amount=Decimal("80.00"),
            payment_status="UNPAID",
        )

        # First settlement
        txn1 = record_order_payment(
            order,
            payment_method="CASH",
            reference="TXN-001",
            recorded_by=self.user_a,
        )
        self.assertEqual(PaymentTransaction.objects.filter(order=order).count(), 1)
        self.assertEqual(order.payment_status, "PAID")
        self.assertIsNotNone(order.paid_at)

        # Retry payment (simulate duplicate click or refresh)
        txn2 = record_order_payment(
            order,
            payment_method="CASH",
            reference="TXN-001",
            recorded_by=self.user_a,
        )
        self.assertEqual(PaymentTransaction.objects.filter(order=order).count(), 1)
        self.assertEqual(txn1.pk, txn2.pk)

        start_dt, end_dt = self._get_day_boundaries(self.today)
        sales = get_sales_report_data(self.restaurant_a, start_dt, end_dt)
        self.assertEqual(sales["gross_collections"], Decimal("80.00"))
        self.assertEqual(sales["order_count"], 1)

    def test_payment_after_midnight_belongs_to_payment_date(self):
        """Revenue reports must use transaction time, not order creation time."""
        yesterday = self.today - timedelta(days=1)

        # Order created yesterday at 23:30
        order = Order.objects.create(
            restaurant=self.restaurant_a,
            table=self.table_a,
            order_type="DINE_IN",
            status="SERVED",
            subtotal=Decimal("120.00"),
            total_amount=Decimal("120.00"),
            payment_status="UNPAID",
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=timezone.make_aware(datetime.combine(yesterday, time(23, 30)), self.tz)
        )

        # Paid today at 00:15
        today_early = timezone.make_aware(datetime.combine(self.today, time(0, 15)), self.tz)
        record_order_payment(
            order,
            payment_method="CARD",
            transaction_at=today_early,
            recorded_by=self.user_a,
        )

        # Yesterday's report should have 0 collections
        y_start, y_end = self._get_day_boundaries(yesterday)
        y_sales = get_sales_report_data(self.restaurant_a, y_start, y_end)
        self.assertEqual(y_sales["gross_collections"], Decimal("0.00"))

        # Today's report should capture the payment
        t_start, t_end = self._get_day_boundaries(self.today)
        t_sales = get_sales_report_data(self.restaurant_a, t_start, t_end)
        self.assertEqual(t_sales["gross_collections"], Decimal("120.00"))
        self.assertEqual(t_sales["net_collections"], Decimal("120.00"))

    def test_cancelled_unpaid_order_excluded(self):
        """Cancelled unpaid orders must be excluded from revenue reports."""
        order = Order.objects.create(
            restaurant=self.restaurant_a,
            table=self.table_a,
            order_type="DINE_IN",
            status=Order.STATUS_CANCELLED,
            subtotal=Decimal("95.00"),
            total_amount=Decimal("95.00"),
            payment_status="UNPAID",
        )

        start_dt, end_dt = self._get_day_boundaries(self.today)
        sales = get_sales_report_data(self.restaurant_a, start_dt, end_dt)
        self.assertEqual(sales["gross_collections"], Decimal("0.00"))
        self.assertEqual(sales["order_count"], 0)

    def test_full_and_partial_refund_reduces_net_collection(self):
        """Refunds create REFUND transactions, decrease net collections, and update refund_amount."""
        order = Order.objects.create(
            restaurant=self.restaurant_a,
            table=self.table_a,
            order_type="DINE_IN",
            status="COMPLETED",
            subtotal=Decimal("100.00"),
            total_amount=Decimal("100.00"),
            payment_status="UNPAID",
        )
        record_order_payment(order, payment_method="CASH")

        # Record partial refund of $30
        record_order_refund(order, amount=Decimal("30.00"), reason="Guest returned side dish")

        order.refresh_from_db()
        self.assertEqual(order.refund_amount, Decimal("30.00"))
        self.assertEqual(order.payment_status, "PAID")

        start_dt, end_dt = self._get_day_boundaries(self.today)
        sales = get_sales_report_data(self.restaurant_a, start_dt, end_dt)
        self.assertEqual(sales["gross_collections"], Decimal("100.00"))
        self.assertEqual(sales["refunds"], Decimal("30.00"))
        self.assertEqual(sales["net_collections"], Decimal("70.00"))

        # Settle remaining refund of $70 (full refund total = $100)
        record_order_refund(order, amount=Decimal("70.00"), reason="Full resolution")
        order.refresh_from_db()
        self.assertEqual(order.refund_amount, Decimal("100.00"))
        self.assertEqual(order.payment_status, "REFUNDED")

        sales_full = get_sales_report_data(self.restaurant_a, start_dt, end_dt)
        self.assertEqual(sales_full["net_collections"], Decimal("0.00"))

    def test_historical_cogs_unchanged_after_later_price_change(self):
        """When ingredient pack price changes later, historical StockTransaction snapshot stays unchanged."""
        order = Order.objects.create(
            restaurant=self.restaurant_a,
            table=self.table_a,
            order_type="DINE_IN",
            status="COMPLETED",
            subtotal=Decimal("50.00"),
            total_amount=Decimal("50.00"),
        )
        record_order_payment(order)

        # Consume 250g of meat at current unit cost ($20/1000g = $0.02/g)
        txn = StockTransaction.objects.create(
            ingredient=self.meat,
            transaction_type=StockTransaction.TransactionType.CONSUMPTION,
            quantity=Decimal("250.000"),
            unit_cost_snapshot=self.meat.current_unit_cost,
            order=order,
        )
        self.assertEqual(txn.unit_cost_snapshot, Decimal("0.020000"))
        self.assertEqual(txn.total_cost, Decimal("5.00"))

        start_dt, end_dt = self._get_day_boundaries(self.today)
        pnl_before = get_profit_and_loss_data(
            self.restaurant_a, self.today, self.today, start_dt, end_dt
        )
        self.assertEqual(pnl_before["cogs"], Decimal("5.00"))

        # Supplier doubles the pack price later
        self.meat.current_pack_price = Decimal("40.00")  # Now $0.04/g
        self.meat.save()

        # The historical transaction unit_cost_snapshot must NOT change
        txn.refresh_from_db()
        self.assertEqual(txn.unit_cost_snapshot, Decimal("0.020000"))
        self.assertEqual(txn.total_cost, Decimal("5.00"))

        # Past P&L report remains exactly $5.00 COGS
        pnl_after = get_profit_and_loss_data(
            self.restaurant_a, self.today, self.today, start_dt, end_dt
        )
        self.assertEqual(pnl_after["cogs"], Decimal("5.00"))

    def test_purchases_not_double_counted_in_pnl(self):
        """Purchases must not be counted alongside COGS in the Profit & Loss statement."""
        # 1. Received a purchase order for $500
        supplier = Supplier.objects.create(
            restaurant=self.restaurant_a, name="Meat Wholesaler"
        )
        po = PurchaseOrder.objects.create(
            restaurant=self.restaurant_a,
            supplier=supplier,
            purchase_date=self.today,
            status=PurchaseOrder.Status.RECEIVED,
            total_amount=Decimal("500.00"),
        )

        # 2. Made sales with $20 COGS
        order = Order.objects.create(
            restaurant=self.restaurant_a,
            table=self.table_a,
            order_type="DINE_IN",
            status="COMPLETED",
            subtotal=Decimal("100.00"),
            total_amount=Decimal("100.00"),
        )
        record_order_payment(order)

        StockTransaction.objects.create(
            ingredient=self.meat,
            transaction_type=StockTransaction.TransactionType.CONSUMPTION,
            quantity=Decimal("1000.000"),
            unit_cost_snapshot=Decimal("0.020000"),
            order=order,
        )

        start_dt, end_dt = self._get_day_boundaries(self.today)
        pnl = get_profit_and_loss_data(
            self.restaurant_a, self.today, self.today, start_dt, end_dt
        )

        # Net sales: $100
        self.assertEqual(pnl["net_sales"], Decimal("100.00"))
        # COGS: $20
        self.assertEqual(pnl["cogs"], Decimal("20.00"))
        # Gross profit: $100 - $20 = $80 (PO of $500 is NOT deducted)
        self.assertEqual(pnl["gross_profit"], Decimal("80.00"))
        # Operating costs do NOT include $500 PO
        self.assertEqual(pnl["operating_expenses"], Decimal("0.00"))
        self.assertEqual(pnl["operating_profit"], Decimal("80.00"))

    def test_paid_payroll_included_and_draft_excluded(self):
        """P&L includes only paid payroll and excludes draft payroll."""
        emp = EmployeeProfile.objects.create(
            user=self.user_a,
            employee_id="EMP-01",
            joining_date=self.today,
            basic_salary=Decimal("3000.00"),
        )

        # Draft payroll (not paid yet)
        p_draft = PayrollRecord.objects.create(
            restaurant=self.restaurant_a,
            employee=emp,
            month=self.today.replace(day=1),
            basic_salary=Decimal("3000.00"),
            net_salary=Decimal("3000.00"),
            status="draft",
        )

        start_dt, end_dt = self._get_day_boundaries(self.today)
        pnl_draft = get_profit_and_loss_data(
            self.restaurant_a, self.today, self.today, start_dt, end_dt
        )
        self.assertEqual(pnl_draft["payroll_cost"], Decimal("0.00"))

        # Mark paid
        p_draft.status = "paid"
        p_draft.paid_at = timezone.now()
        p_draft.save()

        pnl_paid = get_profit_and_loss_data(
            self.restaurant_a, self.today, self.today, start_dt, end_dt
        )
        self.assertEqual(pnl_paid["payroll_cost"], Decimal("3000.00"))

    def test_salary_advance_not_double_counted(self):
        """Salary advance deduction is settled inside net_salary, avoiding double deduction."""
        user_emp = User.objects.create_user(
            username="server_jane",
            password="pass",
            role="waiter",
            restaurant=self.restaurant_a,
        )
        emp = EmployeeProfile.objects.create(
            user=user_emp,
            employee_id="EMP-02",
            joining_date=self.today,
            basic_salary=Decimal("2000.00"),
        )

        # Advance disbursed
        adv = SalaryAdvance.objects.create(
            restaurant=self.restaurant_a,
            employee=emp,
            amount=Decimal("400.00"),
            reason="Emergency",
            status="approved",
        )

        # Payroll settles: Basic 2000 - Advance 400 = Net 1600
        pr = PayrollRecord.objects.create(
            restaurant=self.restaurant_a,
            employee=emp,
            month=self.today.replace(day=1),
            basic_salary=Decimal("2000.00"),
            advance_deduction=Decimal("400.00"),
            net_salary=Decimal("1600.00"),
            status="paid",
            paid_at=timezone.now(),
        )
        adv.status = "deducted"
        adv.payroll_record = pr
        adv.save()

        start_dt, end_dt = self._get_day_boundaries(self.today)
        pnl = get_profit_and_loss_data(
            self.restaurant_a, self.today, self.today, start_dt, end_dt
        )

        # Staff cost is exactly net salary $1600, advance is not added as separate general expense
        self.assertEqual(pnl["payroll_cost"], Decimal("1600.00"))
        self.assertEqual(pnl["operating_expenses"], Decimal("0.00"))

    def test_restaurant_data_isolation(self):
        """Strict multi-tenancy: Restaurant A data never leaks into Restaurant B."""
        # Record $200 sale in Restaurant A
        order_a = Order.objects.create(
            restaurant=self.restaurant_a,
            table=self.table_a,
            order_type="DINE_IN",
            status="COMPLETED",
            subtotal=Decimal("200.00"),
            total_amount=Decimal("200.00"),
        )
        record_order_payment(order_a)

        # Record $50 expense in Restaurant A
        cat_a = ExpenseCategory.objects.create(
            restaurant=self.restaurant_a, name="Utilities"
        )
        Expense.objects.create(
            restaurant=self.restaurant_a,
            category=cat_a,
            title="A Electricity",
            amount=Decimal("50.00"),
            expense_date=self.today,
        )

        # Restaurant B has no sales or expenses
        start_dt, end_dt = self._get_day_boundaries(self.today)
        sales_b = get_sales_report_data(self.restaurant_b, start_dt, end_dt)
        pnl_b = get_profit_and_loss_data(
            self.restaurant_b, self.today, self.today, start_dt, end_dt
        )

        self.assertEqual(sales_b["gross_collections"], Decimal("0.00"))
        self.assertEqual(sales_b["order_count"], 0)
        self.assertEqual(pnl_b["net_sales"], Decimal("0.00"))
        self.assertEqual(pnl_b["operating_expenses"], Decimal("0.00"))
        self.assertEqual(pnl_b["operating_profit"], Decimal("0.00"))

    def test_expense_crud_and_views(self):
        """Back-office user can create, filter, edit, and delete expenses."""
        self.client.force_login(self.user_a)

        # 1. Create Category
        resp_cat = self.client.post(
            "/finance/categories/add/",
            {"name": "Packaging Supplies", "description": "Boxes and bags", "is_active": "on"},
        )
        self.assertEqual(resp_cat.status_code, 302)
        cat = ExpenseCategory.objects.get(name="Packaging Supplies")

        # 2. Create Expense
        resp_exp = self.client.post(
            "/finance/expenses/add/",
            {
                "category": cat.id,
                "title": "Burger Boxes Batch",
                "amount": "150.00",
                "expense_date": self.today.strftime("%Y-%m-%d"),
                "payment_method": "CASH",
                "payee": "Box Co",
            },
        )
        self.assertEqual(resp_exp.status_code, 302)
        exp = Expense.objects.get(title="Burger Boxes Batch")
        self.assertEqual(exp.amount, Decimal("150.00"))

        # 3. View Expense List
        resp_list = self.client.get("/finance/expenses/")
        self.assertEqual(resp_list.status_code, 200)
        self.assertContains(resp_list, "Burger Boxes Batch")

        # 4. Filter by Category
        resp_filter = self.client.get(f"/finance/expenses/?category={cat.id}")
        self.assertEqual(resp_filter.status_code, 200)
        self.assertContains(resp_filter, "Burger Boxes Batch")

        # 5. Delete Expense
        resp_del = self.client.post(f"/finance/expenses/{exp.id}/delete/")
        self.assertEqual(resp_del.status_code, 302)
        self.assertFalse(Expense.objects.filter(id=exp.id).exists())

    def test_all_reports_http_endpoints_render_cleanly(self):
        """Verify all 5 report HTTP views render cleanly for back-office users."""
        self.client.force_login(self.user_a)

        for url in [
            "/finance/reports/sales/",
            "/finance/reports/item-sales/",
            "/finance/reports/stock/",
            "/finance/reports/purchases/",
            "/finance/reports/profit-loss/",
        ]:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, f"Failed for {url}")

    def test_category_margin_aggregation_exact(self):
        """
        Regression test for category gross margin calculation:
        category gross margin = category sales - category historical food cost.
        Example from requirement:
        Sales = 420.00, Food Cost = 349.25 -> Gross Margin = 70.75 (NOT 769.00).
        """
        cat = Category.objects.create(restaurant=self.restaurant_a, name="Burgers")
        menu_item = MenuItem.objects.create(
            category=cat,
            name="Gourmet Burger Combo",
            price=Decimal("420.00"),
            is_available=True,
        )
        # Create recipe with estimated food cost of 349.25
        recipe = Recipe.objects.create(
            menu_item=menu_item,
            instructions="Cook patty and assemble combo",
        )
        # Add ingredient so recipe.estimated_food_cost is 349.25
        ing = Ingredient.objects.create(
            restaurant=self.restaurant_a,
            category=self.ing_cat_a,
            name="Gourmet Patty & Bun",
            base_unit="portion",
            pack_size=1,
            current_pack_price=Decimal("349.25"),
            current_stock=Decimal("100"),
        )
        RecipeIngredient.objects.create(
            recipe=recipe,
            ingredient=ing,
            quantity=Decimal("1"),
        )
        self.assertEqual(recipe.estimated_food_cost, Decimal("349.25"))

        # Create paid order for Restaurant A
        order = Order.objects.create(
            restaurant=self.restaurant_a,
            table=self.table_a,
            order_type="DINE_IN",
            status="COMPLETED",
            subtotal=Decimal("420.00"),
            total_amount=Decimal("420.00"),
            payment_status="PAID",
        )
        OrderItem.objects.create(
            order=order,
            menu_item=menu_item,
            quantity=1,
            price=Decimal("420.00"),
        )
        # Record payment transaction
        record_order_payment(
            order,
            payment_method="CASH",
            reference="TEST-MARGIN",
            recorded_by=self.user_a,
        )

        start_dt, end_dt = self._get_day_boundaries(self.today)
        report_data = get_item_sales_report_data(self.restaurant_a, start_dt, end_dt)

        # Check category summary
        cat_data = report_data["category_summary"].get("Burgers")
        self.assertIsNotNone(cat_data)
        self.assertEqual(cat_data["sales_amount"], Decimal("420.00"))
        self.assertEqual(cat_data["food_cost"], Decimal("349.25"))
        # Category gross margin = 420.00 - 349.25 = 70.75
        self.assertEqual(cat_data["gross_margin"], Decimal("70.75"))
        self.assertNotEqual(cat_data["gross_margin"], Decimal("769.00"))
        self.assertNotEqual(cat_data["gross_margin"], Decimal("769.25"))

        # Verify UI rendering in HTTP endpoint
        self.client.force_login(self.user_a)
        resp = self.client.get("/finance/reports/item-sales/")
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode("utf-8")
        self.assertIn("৳70.75", content)
        self.assertIn("৳420.00", content)
        self.assertIn("৳349.25", content)
        self.assertNotIn("769.00", content)
        self.assertNotIn("769.25", content)
        self.assertNotIn("$", content)

    def test_zero_money_normalization_and_formatting(self):
        """
        Regression test:
        Never display negative zero such as -৳0.00 or ৳-0.00.
        Normalize zero monetary values to ৳0.00.
        """
        from django.template import Context, Template

        # 1. normalize_zero_money
        self.assertEqual(normalize_zero_money(Decimal("0")), Decimal("0.00"))
        self.assertEqual(normalize_zero_money(Decimal("0.00")), Decimal("0.00"))
        self.assertEqual(normalize_zero_money(Decimal("-0.00")), Decimal("0.00"))
        self.assertEqual(normalize_zero_money(Decimal("-0.001")), Decimal("0.00"))
        self.assertEqual(normalize_zero_money(Decimal("-0.0049")), Decimal("0.00"))
        self.assertEqual(normalize_zero_money(0), Decimal("0.00"))
        self.assertEqual(normalize_zero_money(-0.0), Decimal("0.00"))
        self.assertEqual(normalize_zero_money("-0.00"), Decimal("0.00"))
        self.assertEqual(normalize_zero_money(None), Decimal("0.00"))
        self.assertEqual(normalize_zero_money(Decimal("15.50")), Decimal("15.50"))
        self.assertEqual(normalize_zero_money(Decimal("-15.50")), Decimal("-15.50"))

        # 2. format_money
        self.assertEqual(format_money(Decimal("0")), "৳0.00")
        self.assertEqual(format_money(Decimal("0.00")), "৳0.00")
        self.assertEqual(format_money(Decimal("-0.00")), "৳0.00")
        self.assertEqual(format_money(Decimal("-0.001")), "৳0.00")
        self.assertEqual(format_money(Decimal("-0.0049")), "৳0.00")
        self.assertEqual(format_money(0), "৳0.00")
        self.assertEqual(format_money(-0.0), "৳0.00")
        self.assertEqual(format_money("-0.00"), "৳0.00")
        self.assertEqual(format_money(None), "৳0.00")
        self.assertEqual(format_money(Decimal("123.45")), "৳123.45")
        self.assertEqual(format_money(Decimal("-123.45")), "-৳123.45")

        # Confirm negative zero strings are NEVER returned
        self.assertNotEqual(format_money(Decimal("-0.00")), "-৳0.00")
        self.assertNotEqual(format_money(Decimal("-0.00")), "৳-0.00")
        self.assertNotEqual(format_money(-0.0), "-৳0.00")

        # 3. Template filters
        tpl = Template("{% load finance_tags %}{{ val|money }}")
        rendered_neg_zero = tpl.render(Context({"val": Decimal("-0.00")}))
        self.assertEqual(rendered_neg_zero, "৳0.00")
        self.assertNotEqual(rendered_neg_zero, "-৳0.00")
        self.assertNotEqual(rendered_neg_zero, "৳-0.00")

        tpl_bdt = Template("{% load finance_tags %}{{ val|bdt }}")
        self.assertEqual(tpl_bdt.render(Context({"val": Decimal("-0.00")})), "৳0.00")

        tpl_norm = Template("{% load finance_tags %}{{ val|normalize_zero }}")
        self.assertEqual(tpl_norm.render(Context({"val": Decimal("-0.00")})), "0.00")

    def test_all_reports_and_finance_pages_use_bdt_and_no_negative_zero(self):
        """
        Verify that all Finance and Reports views render '৳' consistently,
        never include hardcoded '$', and never display '-৳0.00'.
        """
        self.client.force_login(self.user_a)

        pages = [
            "/finance/reports/sales/",
            "/finance/reports/item-sales/",
            "/finance/reports/stock/",
            "/finance/reports/purchases/",
            "/finance/reports/profit-loss/",
            "/finance/expenses/",
            "/finance/expenses/add/",
            "/finance/categories/",
        ]

        for url in pages:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, f"Failed to load {url}")
            content = resp.content.decode("utf-8")
            self.assertNotIn("$", content, f"Hardcoded '$' found in {url}")
            self.assertNotIn("-৳0.00", content, f"Negative zero '-৳0.00' found in {url}")
            self.assertNotIn("৳-0.00", content, f"Negative zero '৳-0.00' found in {url}")

    def test_opening_balance_excluded_from_purchase_report(self):
        """
        Regression test:
        Opening stock must NOT appear as supplier procurement spend in Purchase Report.
        Purchase Report must remain strictly based on actual RECEIVED purchase orders.
        """
        # Create an OPENING_BALANCE transaction of high value (500 units @ 20.00 = 10,000.00)
        StockTransaction.objects.create(
            ingredient=self.meat,
            restaurant=self.restaurant_a,
            transaction_type=StockTransaction.TransactionType.OPENING_BALANCE,
            quantity=Decimal("500.000"),
            unit_cost_snapshot=Decimal("20.00"),
            note="Initial opening balance / imported stock",
            created_at=timezone.now(),
        )

        # 1. Without any Purchase Orders, total spend must be 0.00
        data_empty = get_purchase_report_data(self.restaurant_a, self.today, self.today)
        self.assertEqual(data_empty["total_spend"], Decimal("0.00"))
        self.assertEqual(data_empty["total_orders_count"], 0)
        self.assertEqual(len(data_empty["supplier_breakdown"]), 0)

        # 2. Add an actual RECEIVED Purchase Order
        supplier = Supplier.objects.create(
            restaurant=self.restaurant_a,
            name="Apex Meats Ltd",
            contact_name="Rahim",
            phone="01700000000",
        )
        po = PurchaseOrder.objects.create(
            restaurant=self.restaurant_a,
            invoice_number="INV-TEST-001",
            supplier=supplier,
            status=PurchaseOrder.Status.RECEIVED,
            purchase_date=self.today,
            received_at=timezone.now(),
            total_amount=Decimal("1500.00"),
        )
        PurchaseOrderItem.objects.create(
            purchase_order=po,
            ingredient=self.meat,
            pack_quantity=Decimal("75.000"),
            pack_price=Decimal("20.00"),
        )

        data_with_po = get_purchase_report_data(self.restaurant_a, self.today, self.today)
        self.assertEqual(data_with_po["total_spend"], Decimal("1500.00"))
        self.assertEqual(data_with_po["total_orders_count"], 1)
        # Opening balance (10,000.00) must NOT be included in total spend
        self.assertNotEqual(data_with_po["total_spend"], Decimal("11500.00"))

        # Verify UI rendering: Purchase Report page shows ৳1500.00, not ৳11,500.00 or opening balance
        self.client.force_login(self.user_a)
        resp = self.client.get("/finance/reports/purchases/")
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode("utf-8")
        self.assertIn("৳1500.00", content)
        self.assertNotIn("11500", content)
        self.assertNotIn("10000", content)

    def test_stock_report_mixed_units_never_summed_together(self):
        """
        Regression test:
        Stock movements must never sum quantities across incompatible units (G, ML, PCS).
        The movement totals must display grouped per unit and transaction counts,
        never a combined single scalar like 'ORDER CONSUMPTION -14624'.
        """
        # Create ingredients with different units in restaurant A
        ing_cat = self.ing_cat_a
        milk = Ingredient.objects.create(
            restaurant=self.restaurant_a,
            category=ing_cat,
            name="Whole Milk",
            sku="MILK-001",
            base_unit=Ingredient.BaseUnit.MILLILITRE,
            pack_size=Decimal("1000.000"),
            current_pack_price=Decimal("80.00"),
            current_stock=Decimal("5000.000"),
        )
        buns = Ingredient.objects.create(
            restaurant=self.restaurant_a,
            category=ing_cat,
            name="Burger Buns",
            sku="BUN-001",
            base_unit=Ingredient.BaseUnit.PIECE,
            pack_size=Decimal("12.000"),
            current_pack_price=Decimal("60.00"),
            current_stock=Decimal("100.000"),
        )

        now = timezone.now()
        start_dt = timezone.make_aware(datetime.combine(self.today, time.min), self.tz)
        end_dt = timezone.make_aware(datetime.combine(self.today, time.max), self.tz)

        # Clear existing transactions for restaurant A within today
        StockTransaction.objects.filter(
            ingredient__restaurant=self.restaurant_a,
            created_at__gte=start_dt,
            created_at__lte=end_dt,
        ).delete()

        # Seed movements across different units:
        # Beef: 12,400 G
        # Milk: 2,200 ML
        # Buns: 24 PCS
        StockTransaction.objects.create(
            ingredient=self.meat,
            restaurant=self.restaurant_a,
            transaction_type=StockTransaction.TransactionType.CONSUMPTION,
            quantity=Decimal("12400.000"),
            unit_cost_snapshot=Decimal("0.020"),
            created_at=now,
        )
        StockTransaction.objects.create(
            ingredient=milk,
            restaurant=self.restaurant_a,
            transaction_type=StockTransaction.TransactionType.CONSUMPTION,
            quantity=Decimal("2200.000"),
            unit_cost_snapshot=Decimal("0.080"),
            created_at=now,
        )
        StockTransaction.objects.create(
            ingredient=buns,
            restaurant=self.restaurant_a,
            transaction_type=StockTransaction.TransactionType.CONSUMPTION,
            quantity=Decimal("24.000"),
            unit_cost_snapshot=Decimal("5.000"),
            created_at=now,
        )

        # Add an OPENING_BALANCE transaction too
        StockTransaction.objects.create(
            ingredient=self.meat,
            restaurant=self.restaurant_a,
            transaction_type=StockTransaction.TransactionType.OPENING_BALANCE,
            quantity=Decimal("5000.000"),
            unit_cost_snapshot=Decimal("0.020"),
            created_at=now,
        )

        # Get stock report data
        data = get_stock_report_data(self.restaurant_a, start_dt, end_dt)
        mov = data["movements_summary"]

        # 1. Consumption must have transaction count 3 and grouped unit quantities
        consumption = mov["consumption"]
        self.assertEqual(consumption["count"], 3)
        self.assertEqual(consumption["units"]["G"], Decimal("12400.000"))
        self.assertEqual(consumption["units"]["ML"], Decimal("2200.000"))
        self.assertEqual(consumption["units"]["PCS"], Decimal("24.000"))

        self.assertIn("12,400 G", consumption["formatted"])
        self.assertIn("2,200 ML", consumption["formatted"])
        self.assertIn("24 PCS", consumption["formatted"])

        # Crucial check: 12,400 + 2,200 + 24 = 14,624 must NEVER be summed into a single number
        for val in consumption.values():
            self.assertNotEqual(val, Decimal("14624"))
            self.assertNotEqual(val, Decimal("14624.000"))
            self.assertNotEqual(val, 14624)
            self.assertNotEqual(val, "-14624")

        # 2. Opening balance must be present in movements summary
        opening = mov["opening_balance"]
        self.assertEqual(opening["count"], 1)
        self.assertEqual(opening["units"]["G"], Decimal("5000.000"))
        self.assertIn("5,000 G", opening["formatted"])

        # 3. Verify UI rendering: Stock Report template
        self.client.force_login(self.user_a)
        resp = self.client.get("/finance/reports/stock/")
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode("utf-8")

        # Must display separate unit lines
        self.assertIn("12,400 G", content)
        self.assertIn("2,200 ML", content)
        self.assertIn("24 PCS", content)

        # Must display transaction count
        self.assertIn("3 txns", content)

        # Must NEVER display mixed sum such as 14,624 or -14624
        self.assertNotIn("14,624", content)
        self.assertNotIn("-14624", content)
        self.assertNotIn("-14,624", content)
        self.assertNotIn("14624", content)

        # Must display preferred card sections
        self.assertIn("Opening Balance", content)
        self.assertIn("Purchases Received", content)
        self.assertIn("Order Consumption", content)
        self.assertIn("Waste &amp; Spoilage", content)
        self.assertIn("Audit Adjustments", content)

