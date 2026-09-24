import importlib
from html import escape
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client, TestCase
from django.urls import reverse

from inventory.models import BranchIngredientStock
from orders.models import Order, PaymentTransaction
from orders.services import record_order_payment, record_order_refund
from orders.test_flow import OrderFlowFixture
from restaurant.models import Branch
from staff.models import StaffNotification
from staff.operations import register_new_order
from users.models import User
from .models import BranchSettings, RestaurantSettings
from .schema import DEFAULTS, FIELDS, SECTIONS
from .services import resolve_settings, validate_new_order


class SettingsRegressionTests(OrderFlowFixture, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.branch = Branch.objects.create(restaurant=cls.restaurant, name='Main', code='MAIN', is_main=True)
        cls.city = Branch.objects.create(restaurant=cls.restaurant, name='City', code='CITY')
        cls.foreign = Branch.objects.create(restaurant=cls.other_restaurant, name='Other', code='O', is_main=True)
        cls.table.branch = cls.branch
        cls.table.save()
        for ingredient in (cls.chicken, cls.salt):
            BranchIngredientStock.objects.filter(branch=cls.branch, ingredient=ingredient).update(current_stock=ingredient.current_stock)

    def setUp(self):
        self.client.force_login(self.owner)

    def configure(self, values, branch=None):
        if branch is not None:
            return BranchSettings.objects.update_or_create(branch=branch, defaults={'values': values})[0]
        return RestaurantSettings.objects.update_or_create(restaurant=self.restaurant, defaults={'values': values})[0]

    def url(self, section='business', scope='restaurant'):
        return reverse('business_settings:settings') + f'?section={section}&scope={scope}'

    def payload(self, section, **updates):
        values = {name: DEFAULTS[name] for name in FIELDS[section]}
        if section == 'business':
            values.update(business_name=self.restaurant.name, business_address=self.restaurant.address, business_phone='')
        values.update(updates)
        # Browser checkboxes omit unchecked values.
        return {key: value for key, value in values.items() if value is not False}

    def test_defaults_preserve_rates_options_currency_and_display(self):
        values = resolve_settings(self.restaurant, self.branch)
        self.assertEqual(values['vat_percent'], '0.00')
        self.assertEqual(values['service_percent'], '0.00')
        self.assertEqual(values['currency'], 'BDT')
        self.assertFalse(values['enforce_hours'])
        self.assertEqual(values['kitchen_sort'], 'newest')
        self.assertEqual(values['kitchen_refresh_seconds'], 0)
        self.assertTrue(values['allow_dine_in'] and values['allow_takeaway'])
        self.assertTrue(values['allow_pay_now'] and values['allow_pay_later'])
        self.assertTrue(all(values['notify_' + key] for key in ('new_order', 'food_ready', 'order_served', 'order_completed', 'task_assigned', 'general')))

    def test_branch_overrides_keep_zero_false_blank_and_never_leak(self):
        self.configure({'vat_percent': '15.00', 'receipt_footer': 'Parent footer', 'payment_card': True})
        self.configure({'vat_percent': '0.00', 'receipt_footer': '', 'payment_card': False}, self.city)
        values = resolve_settings(self.restaurant, self.city)
        self.assertEqual(values['vat_percent'], '0.00')
        self.assertEqual(values['receipt_footer'], '')
        self.assertFalse(values['payment_card'])
        self.assertEqual(resolve_settings(self.restaurant, self.branch)['vat_percent'], '15.00')
        self.assertEqual(resolve_settings(self.other_restaurant, self.foreign)['vat_percent'], '0.00')
        with self.assertRaises(ValidationError):
            resolve_settings(self.restaurant, self.foreign)

    def test_owner_can_render_and_save_each_section(self):
        for section, title in SECTIONS.items():
            response = self.client.get(self.url(section))
            self.assertContains(response, escape(title))
            self.assertEqual(self.client.post(self.url(section), self.payload(section)).status_code, 302, section)
        self.assertEqual(RestaurantSettings.objects.filter(restaurant=self.restaurant).count(), 1)
        self.assertEqual(self.client.get(reverse('owner_dashboard')).status_code, 200)
        self.assertContains(self.client.get(self.url()), reverse('business_settings:settings'))

    def test_owner_only_access_and_foreign_branch_session_falls_back(self):
        manager = User.objects.create_user(username='settings-manager', role='manager', restaurant=self.restaurant, branch=self.branch)
        self.client.force_login(manager)
        self.assertEqual(self.client.get(self.url()).status_code, 403)
        self.assertEqual(self.client.post(self.url('tax'), self.payload('tax')).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(self.url()).status_code, 302)
        self.client.force_login(self.owner)
        session = self.client.session
        session['active_branch_id'] = self.foreign.pk
        session.save()
        self.client.post(self.url('tax', 'branch'), {'override_vat_percent': 'on', 'vat_percent': '8'})
        self.assertTrue(BranchSettings.objects.filter(branch=self.branch).exists())
        self.assertFalse(BranchSettings.objects.filter(branch=self.foreign).exists())

    def test_branch_override_can_be_removed_to_resume_inheritance(self):
        self.configure({'vat_percent': '5.00', 'service_percent': '2.00'})
        url = self.url('tax', 'branch')
        self.assertEqual(self.client.post(url, {'override_vat_percent': 'on', 'vat_percent': '9'}).status_code, 302)
        self.assertEqual(resolve_settings(self.restaurant, self.branch)['vat_percent'], '9')
        self.assertEqual(self.client.post(url, {}).status_code, 302)
        self.assertEqual(resolve_settings(self.restaurant, self.branch)['vat_percent'], '5.00')
        self.assertEqual(BranchSettings.objects.get(branch=self.branch).values, {})

    def test_invalid_rates_timezone_hours_currency_and_options_are_rejected(self):
        for value in ('-1', '101', 'NaN', 'Infinity', '1.001'):
            self.assertEqual(self.client.post(self.url('tax'), self.payload('tax', vat_percent=value)).status_code, 400)
        for updates in ({'timezone': 'No/Such_Timezone'}, {'currency': 'USD'},
                        {'enforce_hours': True, 'opening_time': '09:00', 'closing_time': '09:00'}):
            self.assertEqual(self.client.post(self.url('business'), self.payload('business', **updates)).status_code, 400)
        for updates in ({'allow_dine_in': False, 'allow_takeaway': False}, {'allow_pay_now': False, 'allow_pay_later': False},
                        {'payment_cash': False, 'payment_card': False, 'payment_mobile_banking': False}):
            self.assertEqual(self.client.post(self.url('sales'), self.payload('sales', **updates)).status_code, 400)
        self.assertEqual(self.client.post(self.url('kitchen'), self.payload('kitchen', kitchen_refresh_seconds=1)).status_code, 400)
        self.assertFalse(RestaurantSettings.objects.exists())

    def test_parent_update_cannot_break_inheriting_branch_options(self):
        self.configure({'payment_cash': False, 'payment_card': False}, self.city)
        response = self.client.post(self.url('sales'), self.payload('sales', payment_mobile_banking=False))
        self.assertEqual(response.status_code, 400)
        self.assertTrue(resolve_settings(self.restaurant, self.city)['payment_mobile_banking'])

    def test_qr_and_pos_use_identical_server_rates_and_snapshots(self):
        self.configure({'vat_percent': '10.00', 'service_percent': '5.00'})
        self.configure({'vat_percent': '15.00'}, self.branch)
        self.seed_cart(items=[(self.item_a, 1)])
        self.assertEqual(self.qr_checkout().status_code, 302)
        qr_order = Order.objects.get()
        response = self.pos_checkout(items=[(self.item_a, 1)], service_percent=99, vat_percent=99)
        self.assertEqual(response.status_code, 200)
        pos_order = Order.objects.exclude(pk=qr_order.pk).get()
        for order in (qr_order, pos_order):
            self.assertEqual(order.service_charge, Decimal('5.00'))
            self.assertEqual(order.vat_amount, Decimal('15.75'))
            self.assertEqual(order.total_amount, Decimal('120.75'))
            self.assertEqual(order.settings_snapshot['vat_percent'], '15.00')
            self.assertEqual(order.settings_snapshot['service_percent'], '5.00')
            self.assertEqual(order.settings_snapshot['currency'], 'BDT')
        self.assertEqual(qr_order.settings_snapshot, pos_order.settings_snapshot)

    def test_qr_checkout_preview_and_pos_display_configured_rates(self):
        self.configure({'vat_percent': '7.50', 'service_percent': '2.00'})
        self.seed_cart(items=[(self.item_a, 1)])
        response = self.client.get(reverse('checkout', args=[self.restaurant.pk, self.table.pk]))
        self.assertContains(response, 'VAT (7.50%)')
        self.assertEqual(response.context['pricing']['total_amount'], Decimal('109.65'))
        response = self.client.get(reverse('pos_dashboard'))
        self.assertContains(response, 'value="7.50" readonly')
        self.assertContains(response, 'value="2.00" readonly')

    def test_new_order_toggles_are_enforced_on_server_for_qr_and_pos(self):
        self.configure({'allow_dine_in': False, 'allow_pay_now': False, 'payment_cash': False})
        self.seed_cart(items=[(self.item_a, 1)])
        self.assertEqual(self.qr_checkout().status_code, 400)
        self.assertEqual(self.pos_checkout(items=[(self.item_a, 1)], payment_timing='PAY_NOW', payment_method='CARD').status_code, 400)
        self.assertFalse(Order.objects.exists())
        self.configure({'payment_cash': False})
        response = self.client.post(reverse('checkout', args=[self.restaurant.pk, self.table.pk]), {
            'order_type': 'DINE_IN', 'payment_timing': 'PAY_NOW', 'payment_method': 'CASH'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.pos_checkout(items=[(self.item_a, 1)], payment_timing='PAY_NOW', payment_method='CASH').status_code, 400)
        self.assertFalse(Order.objects.exists())
        self.assertFalse(PaymentTransaction.objects.exists())

    def test_payment_options_apply_to_later_collection_without_breaking_refunds(self):
        order = self.create_order(reserve=False)
        self.configure({'payment_cash': False})
        with self.assertRaises(ValidationError):
            record_order_payment(order, payment_method='CASH')
        txn = record_order_payment(order, payment_method='CARD')
        self.configure({'payment_card': False})
        self.assertEqual(record_order_payment(order, payment_method='CARD').pk, txn.pk)
        refund = record_order_refund(order, Decimal('10.00'), payment_method='CARD')
        self.assertEqual(refund.amount, Decimal('10.00'))

    def test_receipts_freeze_branding_contact_footer_timezone_and_amounts(self):
        self.configure({'receipt_name': 'Original Brand', 'receipt_contact': 'Original contact', 'receipt_footer': 'Original footer',
                        'receipt_logo_url': 'https://example.com/logo.png', 'receipt_show_branch': True,
                        'tax_registration': 'VAT-123', 'vat_percent': '10.00', 'timezone': 'Asia/Dhaka'})
        self.seed_cart(items=[(self.item_a, 1)])
        self.qr_checkout()
        order = Order.objects.get()
        snapshot = dict(order.settings_snapshot)
        amount = order.total_amount
        self.configure({'receipt_name': 'Changed Brand', 'receipt_contact': 'Changed contact', 'receipt_footer': 'Changed footer', 'vat_percent': '50.00'})
        self.restaurant.name = 'Changed restaurant'
        self.restaurant.save()
        self.branch.name = 'Changed branch'
        self.branch.save()
        order.refresh_from_db()
        order.status = 'SERVED'
        order.save()
        for endpoint in ('order_receipt', 'bill_preview'):
            response = self.client.get(reverse(endpoint, args=[order.pk]))
            self.assertContains(response, 'Original Brand')
            self.assertContains(response, 'Original contact')
            self.assertContains(response, 'Original footer')
            self.assertContains(response, 'VAT-123')
            self.assertContains(response, 'https://example.com/logo.png')
            self.assertNotContains(response, 'Changed Brand')
            self.assertEqual(response.context['receipt_settings'], snapshot)
        order.refresh_from_db()
        self.assertEqual(order.settings_snapshot, snapshot)
        self.assertEqual(order.total_amount, amount)

    def test_backfill_keeps_unknown_rates_and_stored_totals(self):
        order = self.create_order(reserve=False)
        Order.objects.filter(pk=order.pk).update(settings_snapshot={}, vat_amount=Decimal('37.12'), total_amount=Decimal('317.12'))
        migration = importlib.import_module('orders.migrations.0012_backfill_receipt_settings')
        migration.freeze_existing_receipts(apps, SimpleNamespace(connection=connection))
        order.refresh_from_db()
        self.assertIsNone(order.settings_snapshot['vat_percent'])
        self.assertIsNone(order.settings_snapshot['service_percent'])
        self.assertEqual(order.vat_amount, Decimal('37.12'))
        self.assertEqual(order.total_amount, Decimal('317.12'))
        self.assertEqual(order.settings_snapshot['receipt_name'], self.restaurant.name)
        original = dict(order.settings_snapshot)
        self.configure({'vat_percent': '90.00'})
        migration.freeze_existing_receipts(apps, SimpleNamespace(connection=connection))
        order.refresh_from_db()
        self.assertEqual(order.settings_snapshot, original)

    def test_timezone_and_overnight_hours_gate_only_new_orders(self):
        values = {**DEFAULTS, 'timezone': 'Asia/Dhaka', 'enforce_hours': True, 'opening_time': '20:00', 'closing_time': '04:00'}
        for hour in (20, 23, 0, 3):
            now = datetime(2026, 9, 22, hour, tzinfo=ZoneInfo('Asia/Dhaka'))
            validate_new_order(values, 'DINE_IN', 'PAY_LATER', now=now)
        for hour in (4, 12, 19):
            with self.assertRaises(ValidationError):
                validate_new_order(values, 'DINE_IN', 'PAY_LATER', now=datetime(2026, 9, 22, hour, tzinfo=ZoneInfo('Asia/Dhaka')))
        self.configure({key: values[key] for key in ('timezone', 'enforce_hours', 'opening_time', 'closing_time')})
        with patch('business_settings.services.timezone.now', return_value=datetime(2026, 9, 22, 6, tzinfo=ZoneInfo('UTC'))):
            self.seed_cart(items=[(self.item_a, 1)])
            self.assertEqual(self.qr_checkout().status_code, 400)
            self.assertEqual(self.pos_checkout(items=[(self.item_a, 1)]).status_code, 400)
        order = self.create_order(reserve=False)
        self.assertIsNotNone(record_order_payment(order, payment_method='CASH'))

    def test_kitchen_preferences_change_display_and_estimate_not_order_status(self):
        first = self.create_order(reserve=False)
        second = self.create_order(reserve=False)
        self.configure({'kitchen_sort': 'oldest', 'kitchen_refresh_seconds': 15, 'prep_estimate_minutes': 42})
        response = self.client.get(reverse('kitchen_dashboard'))
        self.assertEqual([order.pk for order in response.context['orders']], [first.pk, second.pk])
        self.assertContains(response, 'window.location.reload()')
        from menu.tracking_views import estimate_order_minutes
        self.assertEqual(estimate_order_minutes(first), 42)
        first.refresh_from_db()
        self.assertEqual(first.status, 'NEW')
        self.configure({'kitchen_show_dine_in': False})
        self.assertFalse(self.client.get(reverse('kitchen_dashboard')).context['orders'])

    def test_notification_preferences_inherit_and_apply_only_to_new_events(self):
        first = self.create_order(reserve=False)
        register_new_order(first.pk)
        self.assertTrue(StaffNotification.objects.filter(order=first, recipient=self.owner).exists())
        self.configure({'notify_new_order': False})
        second = self.create_order(reserve=False)
        register_new_order(second.pk)
        self.assertFalse(StaffNotification.objects.filter(order=second).exists())
        self.assertTrue(StaffNotification.objects.filter(order=first).exists())
        self.configure({'notify_new_order': True}, self.branch)
        register_new_order(second.pk)
        register_new_order(second.pk)
        self.assertEqual(StaffNotification.objects.filter(order=second, recipient=self.owner).count(), 1)

    def test_csrf_and_unsupported_settings_are_rejected(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.post(self.url('tax'), self.payload('tax')).status_code, 403)
        row = RestaurantSettings(restaurant=self.restaurant, values={'unknown_setting': True})
        with self.assertRaises(ValidationError):
            row.full_clean()
