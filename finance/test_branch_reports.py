from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from finance.models import Expense, ExpenseCategory
from inventory.models import (BranchIngredientStock, Ingredient, IngredientCategory, PurchaseOrder,
                              PurchaseOrderItem, StockTransaction, WasteRecord)
from menu.models import Category, MenuItem
from orders.models import Order, OrderItem, PaymentTransaction
from restaurant.models import Branch, Restaurant
from staff.models import EmployeeProfile, PayrollRecord


class BranchReportingTests(TestCase):
    reports = ('sales_report', 'item_sales_report', 'stock_report',
               'purchase_report', 'profit_loss_report')

    def setUp(self):
        self.restaurant = Restaurant.objects.create(name='Bistro')
        self.a = Branch.objects.create(restaurant=self.restaurant, name='Main', code='A', is_main=True)
        self.b = Branch.objects.create(restaurant=self.restaurant, name='City', code='B')
        other = Restaurant.objects.create(name='Other')
        self.foreign = Branch.objects.create(restaurant=other, name='Other', code='O', is_main=True)
        self.owner = get_user_model().objects.create_user(username='owner', role='owner', restaurant=self.restaurant)
        self.staff = get_user_model().objects.create_user(username='staff', role='manager', restaurant=self.restaurant, branch=self.b)
        self.client.force_login(self.owner)
        self.today = timezone.localdate()
        for branch, amount in ((self.a, 100), (self.b, 200), (self.foreign, 900)):
            restaurant = branch.restaurant
            category, _ = Category.objects.get_or_create(restaurant=restaurant, name='Food')
            menu_item = MenuItem.objects.create(category=category, name=branch.name, price=amount)
            order = Order.objects.create(restaurant=restaurant, branch=branch, order_type='TAKEAWAY', status='SERVED', subtotal=amount, total_amount=amount)
            OrderItem.objects.create(order=order, menu_item=menu_item, quantity=1, price=amount)
            for kind, value in (('PAYMENT', amount), ('REFUND', amount / 10)):
                PaymentTransaction.objects.create(restaurant=restaurant, branch=branch, order=order, transaction_type=kind, amount=value, payment_method='CASH')
            ing_category, _ = IngredientCategory.objects.get_or_create(restaurant=restaurant, name="Food")
            ingredient = Ingredient.objects.create(category=ing_category, restaurant=restaurant, name=branch.name, sku=branch.code, base_unit='PCS', pack_size=1, current_pack_price=2, current_stock=999)
            BranchIngredientStock.objects.filter(ingredient=ingredient).update(current_stock=0)
            BranchIngredientStock.objects.filter(ingredient=ingredient, branch=branch).update(current_stock=amount, reserved_stock=10, min_stock_alert=150)
            for kind in ('CONSUMPTION', 'PURCHASE'):
                StockTransaction.objects.create(restaurant=restaurant, branch=branch, ingredient=ingredient, transaction_type=kind, quantity=amount / 10, unit_cost_snapshot=1)
            WasteRecord.objects.create(restaurant=restaurant, branch=branch, ingredient=ingredient, quantity=amount / 100, unit_cost_snapshot=1, reason='OTHER')
            po = PurchaseOrder.objects.create(restaurant=restaurant, branch=branch, purchase_date=self.today, status='RECEIVED', total_amount=amount)
            PurchaseOrderItem.objects.create(purchase_order=po, ingredient=ingredient, pack_quantity=1, pack_price=amount, total_price=amount)
            category, _ = ExpenseCategory.objects.get_or_create(restaurant=restaurant, name='Rent')
            Expense.objects.create(restaurant=restaurant, branch=branch, category=category, title='Rent', amount=amount / 10)
            employee_user = get_user_model().objects.create_user(username='employee' + branch.code, restaurant=restaurant, branch=branch)
            employee = EmployeeProfile.objects.create(user=employee_user, employee_id=branch.code, joining_date=self.today)
            PayrollRecord.objects.create(restaurant=restaurant, branch=branch, employee=employee, month=self.today.replace(day=1), basic_salary=amount / 5, net_salary=amount / 5, status='paid', paid_at=timezone.now())
        category = ExpenseCategory.objects.get(restaurant=self.restaurant, name='Rent')
        expense = Expense.objects.create(restaurant=self.restaurant, category=category, title='Company', amount=50)
        # Existing save() defaults missing branches; represent an explicitly unscoped record.
        Expense.objects.filter(pk=expense.pk).update(branch=None)

    def report(self, name, **params):
        return self.client.get(reverse('finance:' + name), {'preset': 'today', **params})

    def select(self, branch):
        session = self.client.session
        session['active_branch_id'] = branch.pk
        session.save()

    def test_all_reports_follow_session_and_consolidate_without_other_tenant(self):
        for branch, scale in ((self.a, 1), (self.b, 2), (None, 3)):
            if branch:
                self.select(branch)
            params = {} if branch else {'scope': 'all'}
            responses = {name: self.report(name, **params) for name in self.reports}
            for response in responses.values():
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'Branch: ' + branch.name if branch else 'All Branches — Consolidated')
            sales = responses['sales_report'].context
            self.assertEqual(sales['gross_collections'], 100 * scale)
            self.assertEqual(sales['refunds'], 10 * scale)
            self.assertEqual(sales['net_sales'], 90 * scale)
            self.assertEqual(sum(t.amount for t in sales['transactions']), 110 * scale)
            self.assertEqual(sales['payment_method_breakdown']['Cash']['net'], 90 * scale)
            items = responses['item_sales_report'].context
            self.assertEqual(items['total_sales'], 100 * scale)
            stock = responses['stock_report'].context
            self.assertEqual(stock['total_valuation'], 200 * scale)
            self.assertEqual(stock['movements_summary']['consumption']['units']['PCS'], 10 * scale)
            self.assertEqual(sum(row['reserved_stock'] for row in stock['stock_items']), 10 if branch else 20)
            purchases = responses['purchase_report'].context
            self.assertEqual(purchases['total_spend'], 100 * scale)
            self.assertEqual(sum(row['total_spend'] for row in purchases['ingredient_breakdown'].values()), 100 * scale)
            pnl = responses['profit_loss_report'].context
            for key, value in [('net_sales', 90), ('cogs', 10), ('waste_loss', 1), ('payroll_cost', 20)]:
                self.assertEqual(pnl[key], value * scale)
            self.assertEqual(pnl['operating_expenses'], 10 * scale + (50 if branch is None else 0))
            self.assertEqual(pnl['operating_profit'], 49 * scale - (50 if branch is None else 0))

    def test_staff_cannot_consolidate_or_override_assigned_branch(self):
        self.client.force_login(self.staff)
        self.select(self.a)
        for name in self.reports:
            response = self.report(name, branch=self.a.pk)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context['report_branch'], self.b)
            self.assertEqual(self.report(name, scope='all').status_code, 403)

    def test_foreign_and_stale_sessions_use_existing_branch_fallback(self):
        for branch_id in (self.foreign.pk, 999999):
            session = self.client.session
            session['active_branch_id'] = branch_id
            session.save()
            for name in self.reports:
                response = self.report(name)
                self.assertEqual(response.context['report_branch'], self.a)

    def test_scope_survives_date_filters_without_changing_session(self):
        self.select(self.b)
        for name in self.reports:
            response = self.report(name, scope='all')
            self.assertContains(response, '?preset=yesterday&amp;scope=all')
            self.assertContains(response, 'value="all" selected')
        self.assertEqual(self.client.session['active_branch_id'], self.b.pk)

    def test_missing_branch_or_restaurant_never_defaults_to_consolidated(self):
        empty = Restaurant.objects.create(name='Empty')
        self.owner.restaurant = empty
        self.owner.save()
        for name in self.reports:
            self.assertEqual(self.report(name).status_code, 403)
        self.owner.restaurant = None
        self.owner.save()
        for name in self.reports:
            self.assertEqual(self.report(name, scope='all').status_code, 403)
