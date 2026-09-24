from datetime import datetime, timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from dashboard.views import _order_age_metadata
from orders.models import Order
from orders.services import transition_order_status
from orders.test_flow import OrderFlowFixture
from restaurant.models import Restaurant
from users.models import User


@override_settings(TIME_ZONE="Asia/Dhaka")
class OrdersTimingTests(TestCase):
    def setUp(self):
        self.now = timezone.make_aware(datetime(2026, 9, 18, 10))
        self.restaurant = Restaurant.objects.create(name="Timing restaurant")
        self.dev_user = User.objects.create_user(
            username="timing-user", role="owner", restaurant=self.restaurant
        )
        self.client.force_login(self.dev_user)
        clock = patch("django.utils.timezone.now", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def order(self, status, minutes, *, created_at=None, known_stage=True):
        order = Order.objects.create(
            restaurant=self.restaurant, order_type="TAKEAWAY",
            status=status, total_amount=0,
        )
        started_at = self.now - timedelta(minutes=minutes)
        Order.objects.filter(pk=order.pk).update(
            created_at=created_at or started_at,
            status_changed_at=started_at if known_stage and status != "NEW" else None,
        )
        order.refresh_from_db()
        return order

    def test_threshold_boundaries_and_stage_ages(self):
        for status, threshold in [("NEW", 10), ("ACCEPTED", 15), ("PREPARING", 30), ("READY", 5)]:
            for seconds, expected in [(-1, False), (0, True), (1, True)]:
                with self.subTest(status=status, seconds=seconds):
                    order = self.order(status, threshold)
                    started_at = self.now - timedelta(minutes=threshold, seconds=seconds)
                    if status == "NEW":
                        order.created_at = started_at
                    else:
                        order.status_changed_at = started_at
                        order.created_at = self.now - timedelta(hours=2)
                    _order_age_metadata(order, self.now, threshold)
                    self.assertEqual(order.is_urgent, expected and status == "READY")
                    self.assertEqual(order.is_delayed, expected and status != "READY")

    def test_page_uses_stage_timing_and_friendly_labels(self):
        old_creation = self.now - timedelta(hours=2)
        self.order("NEW", 8)
        self.order("PREPARING", 24, created_at=old_creation)
        self.order("ACCEPTED", 14, created_at=old_creation)
        delayed = self.order("ACCEPTED", 15, created_at=old_creation)
        long_cooking = self.order("PREPARING", 30, created_at=old_creation)
        ready = self.order("READY", 6, created_at=old_creation)
        fresh_ready = self.order("READY", 1, created_at=old_creation)
        response = self.client.get(reverse("orders_list"))
        self.assertEqual(response.status_code, 200)
        for label in ["Waiting 8 min", "Cooking 24 min", "Ready 6 min ago", "Ready 1 min ago"]:
            self.assertContains(response, label)
        self.assertEqual([o.pk for o in response.context["accepted_attention"]], [delayed.pk])
        self.assertEqual([o.pk for o in response.context["preparing_attention"]], [long_cooking.pk])
        ready_orders = {o.pk: o for o in response.context["ready_orders"]}
        self.assertTrue(ready_orders[ready.pk].is_urgent)
        self.assertFalse(ready_orders[fresh_ready.pk].is_urgent)
        self.assertEqual(response.context["attention_count"], 5)

    def test_previous_day_orders_are_stale_even_with_recent_status_change(self):
        # Only 10.5 hours old, and from the previous local day (same UTC date).
        previous_day = self.now - timedelta(hours=10, minutes=30)
        stale_ids = {
            self.order(status, 1, created_at=previous_day).pk
            for status in ["NEW", "ACCEPTED", "PREPARING", "READY"]
        }
        response = self.client.get(reverse("orders_list"))
        self.assertEqual(response.context["stale_count"], 4)
        self.assertEqual(response.context["attention_count"], 0)
        self.assertEqual(response.context["page_obj"].paginator.count, 0)
        for key in ["new_count", "accepted_count", "preparing_count", "ready_count"]:
            self.assertEqual(response.context[key], 0)
        review = self.client.get(reverse("stale_orders_list"))
        self.assertEqual({o.pk for o in review.context["page_obj"]}, stale_ids)

    def test_same_day_twelve_hour_cutoff_and_finished_orders(self):
        self.now = self.now.replace(hour=23)
        with patch("django.utils.timezone.now", return_value=self.now):
            self.order("NEW", 721)
            self.order("NEW", 720)
            for status in ["SERVED", "COMPLETED"]:
                order = self.order(status, 90)
                _order_age_metadata(order, self.now, 5)
                self.assertFalse(order.is_delayed)
                self.assertFalse(order.is_urgent)
            response = self.client.get(reverse("orders_list"))
        self.assertEqual(response.context["stale_count"], 1)
        self.assertEqual(response.context["attention_count"], 1)
        self.assertEqual(response.context["page_obj"].paginator.count, 3)

    def test_legacy_stage_timing_is_explicit(self):
        self.order("READY", 40, known_stage=False)
        response = self.client.get(reverse("orders_list"))
        self.assertContains(response, "Received 40 min ago (stage timing unavailable)")
        self.assertNotContains(response, "Ready 40 min ago")

    def test_realistic_order_age_formatting(self):
        order_fresh = self.order("NEW", 0)
        _order_age_metadata(order_fresh, self.now)
        self.assertEqual(order_fresh.age_label, "Just now")
        self.assertEqual(order_fresh.timing_label, "Waiting less than 1 min")

        order_8m = self.order("NEW", 8)
        _order_age_metadata(order_8m, self.now)
        self.assertEqual(order_8m.age_label, "8 min")
        self.assertEqual(order_8m.timing_label, "Waiting 8 min")

        order_80m = self.order("PREPARING", 80)
        _order_age_metadata(order_80m, self.now)
        self.assertEqual(order_80m.age_label, "1 hr 20 min")
        self.assertEqual(order_80m.timing_label, "Cooking 1 hr 20 min")

        order_yesterday = self.order("READY", 1440, created_at=self.now - timedelta(days=1, minutes=10))
        _order_age_metadata(order_yesterday, self.now)
        self.assertEqual(order_yesterday.age_label, "Yesterday")
        self.assertEqual(order_yesterday.timing_label, "Ready since Yesterday")

        order_2days = self.order("READY", 2880, created_at=self.now - timedelta(days=2))
        _order_age_metadata(order_2days, self.now)
        self.assertEqual(order_2days.age_label, "2 days ago")
        self.assertEqual(order_2days.timing_label, "Ready 2 days ago")



class OrderTransitionTimingTests(OrderFlowFixture, TestCase):
    def test_transition_records_time_without_resetting_on_retry(self):
        self.now = timezone.now()
        clock = patch("django.utils.timezone.now", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        order = self.create_order()
        transition_order_status(order, "ACCEPTED")
        order.refresh_from_db()
        self.assertEqual(order.status_changed_at, self.now)
        with patch("django.utils.timezone.now", return_value=self.now + timedelta(minutes=3)):
            transition_order_status(order, "ACCEPTED")
        order.refresh_from_db()
        self.assertEqual(order.status_changed_at, self.now)


class OrderDetailActionsTests(OrderFlowFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_actions_and_timeline_for_each_status(self):
        actions = {
            "NEW": ("ACCEPTED", "Accept Order"),
            "ACCEPTED": (None, "Open Kitchen"),
            "PREPARING": (None, "Open Kitchen"),
            "READY": ("SERVED", "Mark Served"),
            "SERVED": ("COMPLETED", "Complete Order"),
            "COMPLETED": (None, None),
        }
        now = timezone.now()
        for index, (status, (target, label)) in enumerate(actions.items()):
            with self.subTest(status=status):
                order = self.create_order(status=status, reserve=False)
                Order.objects.filter(pk=order.pk).update(
                    created_at=now - timedelta(minutes=8), payment_status="PAID",
                )
                with patch("django.utils.timezone.now", return_value=now):
                    response = self.client.get(reverse("order_detail", args=[order.pk]))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Current Status")
                self.assertContains(response, "Order Age")
                self.assertContains(response, "8 min")
                self.assertContains(response, "Payment Status")
                self.assertContains(response, "Paid")
                self.assertContains(response, 'aria-current="step"', count=1)
                self.assertEqual(
                    [step["state"] for step in response.context["status_timeline"]],
                    ["done"] * index + ["current"] + ["upcoming"] * (5 - index),
                )
                if target:
                    self.assertContains(response, f'name="status" value="{target}"', count=1)
                    self.assertEqual(response.context["status_choices"], [(target, label)])
                else:
                    self.assertNotContains(response, 'name="status"')
                if status in {"ACCEPTED", "PREPARING"}:
                    self.assertContains(response, f'href="{reverse("kitchen_dashboard")}" class="update-btn kitchen-link">Open Kitchen</a>')
                if status == "COMPLETED":
                    self.assertContains(response, "✓ Order Completed")
                    self.assertNotContains(response, 'class="update-btn')
                else:
                    self.assertContains(response, label)
                    self.assertNotContains(response, "✓ Order Completed")

    def test_forged_skips_and_kitchen_actions_are_rejected(self):
        for source, target in [
            ("NEW", "SERVED"), ("NEW", "COMPLETED"),
            ("ACCEPTED", "PREPARING"), ("ACCEPTED", "SERVED"),
            ("PREPARING", "READY"), ("PREPARING", "COMPLETED"),
            ("READY", "COMPLETED"), ("COMPLETED", "NEW"),
        ]:
            with self.subTest(source=source, target=target):
                order = self.create_order(status=source, reserve=False)
                response = self.client.post(
                    reverse("order_detail", args=[order.pk]), {"status": target}, follow=True,
                )
                order.refresh_from_db()
                self.assertEqual(order.status, source)
                self.assertIsNone(order.status_changed_at)
                self.assertFalse(order.stock_transactions.exists())
                self.assertContains(response, 'role="alert"')

    def test_valid_detail_actions_preserve_inventory_flow(self):
        order = self.create_order()
        url = reverse("order_detail", args=[order.pk])
        self.assertRedirects(self.client.post(url, {"status": "ACCEPTED"}), url)
        order.refresh_from_db()
        self.assertEqual(order.status, "ACCEPTED")
        self.assert_inventory(order)
        transition_order_status(order, "PREPARING")
        transition_order_status(order, "READY")
        for target in ["SERVED", "COMPLETED"]:
            # The payment guard requires PAID before COMPLETED.
            if target == "COMPLETED":
                Order.objects.filter(pk=order.pk).update(payment_status="PAID")
            self.assertRedirects(self.client.post(url, {"status": target}), url)
            order.refresh_from_db()
            self.assertEqual(order.status, target)
            self.assert_inventory(order, consumed=True)



@override_settings(TIME_ZONE="Asia/Dhaka")
class StaleOrderReviewTests(OrderFlowFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.staff_user = User.objects.create_user(
            username="stale-review-staff", role="waiter", restaurant=self.restaurant
        )
        self.client.force_login(self.staff_user)
        self.now = timezone.make_aware(datetime(2026, 9, 18, 10))
        clock = patch("django.utils.timezone.now", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def make_stale(self, order, age=timedelta(days=2, hours=3, minutes=4)):
        Order.objects.filter(pk=order.pk).update(created_at=self.now - age)
        return order

    def test_review_shows_age_status_reasons_and_active_reservations(self):
        order = self.make_stale(self.create_order())
        response = self.client.get(reverse("stale_orders_list"))
        self.assertEqual(response.status_code, 200)
        for text in [
            "2d 3h 4m", "New", "Unfinished order from a previous day",
            "Open for more than 12 hours", "Chicken", "250", "Active",
            "stock held", "No consumption transactions recorded", "Read-only order review",
        ]:
            self.assertContains(response, text)
        self.assertNotContains(response, reverse("order_detail", args=[order.pk]))
        self.assertNotContains(response, 'method="POST"')
        for label in ["Accept Order", "Mark Served", "Complete Order"]:
            self.assertNotContains(response, label)
        self.assert_inventory(order)
        order.refresh_from_db()
        self.assertEqual(order.status, "NEW")

    def test_consumed_reservations_and_consumption_transactions(self):
        order = self.create_order()
        transition_order_status(order, "ACCEPTED")
        transition_order_status(order, "PREPARING")
        self.make_stale(order)
        response = self.client.get(reverse("stale_orders_list"))
        self.assertContains(response, "Preparing")
        self.assertContains(response, "Consumed")
        self.assertContains(response, "Yes — 2 consumption transactions recorded")
        self.assertEqual(response.context["page_obj"][0].consumption_count, 2)
        self.assert_inventory(order, consumed=True)

    def test_missing_and_released_reservations_and_non_consumption_ledger(self):
        from inventory.models import StockTransaction
        from inventory.services import release_order_reservations

        missing = self.make_stale(self.create_order(reserve=False))
        released = self.make_stale(self.create_order())
        release_order_reservations(released)
        StockTransaction.objects.create(
            order=missing, ingredient=self.chicken, transaction_type="ADJUSTMENT_IN", quantity=1,
        )
        response = self.client.get(reverse("stale_orders_list"))
        self.assertContains(response, "No reservation records")
        self.assertContains(response, "Released")
        self.assertContains(response, "hold released")
        self.assertContains(response, "No consumption transactions recorded", count=2)
        self.assertTrue(all(o.consumption_count == 0 for o in response.context["page_obj"]))

    def test_previous_day_flag_does_not_claim_twelve_hours(self):
        self.make_stale(self.create_order(), timedelta(hours=11))
        response = self.client.get(reverse("stale_orders_list"))
        order = response.context["page_obj"][0]
        self.assertEqual(order.flag_reason, "Unfinished order from a previous day")
        self.assertEqual(order.review_age_label, "11h")

    def test_same_day_flag_and_filters(self):
        now = self.now.replace(hour=23)
        order = self.create_order()
        Order.objects.filter(pk=order.pk).update(created_at=now - timedelta(hours=13))
        with patch("django.utils.timezone.now", return_value=now):
            response = self.client.get(reverse("stale_orders_list"), {"search": order.pk, "status": "NEW"})
        self.assertEqual(response.context["page_obj"][0].flag_reason, "Open for more than 12 hours")

    def test_write_methods_are_rejected_without_changing_order_or_inventory(self):
        order = self.make_stale(self.create_order())
        for method in ["post", "put", "patch", "delete"]:
            response = getattr(self.client, method)(reverse("stale_orders_list"), {"status": "ACCEPTED"})
            self.assertEqual(response.status_code, 405)
        order.refresh_from_db()
        self.assertEqual(order.status, "NEW")
        self.assertIsNone(order.status_changed_at)
        self.assert_inventory(order)


@override_settings(TIME_ZONE="Asia/Dhaka")
class StaleOrderArchivingTests(OrderFlowFixture, TestCase):
    def setUp(self):
        from decimal import Decimal
        from django.contrib.auth import get_user_model
        from inventory.models import StockReservation, StockTransaction
        from restaurant.models import Table

        self.Decimal = Decimal
        self.StockReservation = StockReservation
        self.StockTransaction = StockTransaction
        self.Table = Table

        self.now = timezone.make_aware(datetime(2026, 9, 18, 10))
        clock = patch("django.utils.timezone.now", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)

        User = get_user_model()
        self.manager = User.objects.create_user(
            username="stale-mgr", role="manager", restaurant=self.restaurant
        )
        self.waiter = User.objects.create_user(
            username="stale-waiter", role="waiter", restaurant=self.restaurant
        )

    def make_stale(self, order, age=timedelta(days=1, hours=2)):
        Order.objects.filter(pk=order.pk).update(created_at=self.now - age)
        return order

    def test_archive_requires_manager_or_admin(self):
        order = self.make_stale(self.create_order())
        url = reverse("archive_stale_order", args=[order.pk])
        resp = self.client.post(url)
        self.assertRedirects(resp, reverse("users:login"))
        order.refresh_from_db()
        self.assertEqual(order.status, "NEW")

        self.client.force_login(self.waiter)
        resp = self.client.post(url)
        self.assertRedirects(resp, reverse("users:landing"))
        order.refresh_from_db()
        self.assertEqual(order.status, "NEW")

    def test_archive_order_with_active_reservations_releases_hold_and_frees_table(self):
        self.table.mark_occupied()
        order = self.make_stale(self.create_order())
        self.client.force_login(self.manager)
        url = reverse("archive_stale_order", args=[order.pk])
        resp = self.client.post(url)
        self.assertRedirects(resp, reverse("stale_orders_list"))

        order.refresh_from_db()
        self.assertEqual(order.status, "CANCELLED")
        for res in order.stock_reservations.all():
            self.assertEqual(res.status, self.StockReservation.Status.RELEASED)
        self.chicken.refresh_from_db()
        self.assertEqual(self.chicken.current_stock, self.Decimal("1000"))
        self.assertEqual(self.chicken.reserved_stock, self.Decimal("0"))
        self.assertEqual(self.chicken.available_stock, self.Decimal("1000"))

        self.table.refresh_from_db()
        self.assertFalse(self.table.is_occupied)
        self.assertEqual(self.table.status, self.Table.STATUS_AVAILABLE)

    def test_archive_order_with_consumption_preserves_ledger(self):
        order = self.create_order()
        transition_order_status(order, "ACCEPTED")
        transition_order_status(order, "PREPARING")
        self.make_stale(order)
        self.client.force_login(self.manager)

        url = reverse("archive_stale_order", args=[order.pk])
        resp = self.client.post(url)
        self.assertRedirects(resp, reverse("stale_orders_list"))

        order.refresh_from_db()
        self.assertEqual(order.status, "CANCELLED")
        txs = list(order.stock_transactions.filter(
            transaction_type=self.StockTransaction.TransactionType.CONSUMPTION
        ))
        self.assertEqual(len(txs), 2)

    def test_archive_all_stale_orders(self):
        self.table.mark_occupied()
        order1 = self.make_stale(self.create_order())
        order2 = self.make_stale(self.create_order())
        self.client.force_login(self.manager)

        resp = self.client.post(reverse("archive_all_stale_orders"))
        self.assertRedirects(resp, reverse("stale_orders_list"))

        order1.refresh_from_db()
        order2.refresh_from_db()
        self.assertEqual(order1.status, "CANCELLED")
        self.assertEqual(order2.status, "CANCELLED")
        self.table.refresh_from_db()
        self.assertFalse(self.table.is_occupied)


class SidebarAndNavigationTests(OrderFlowFixture, TestCase):
    """Tests verifying role-aware unified sidebar, no broken links, and cross navigation."""

    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model
        User = get_user_model()
        self.manager = User.objects.create_user(
            username="nav-manager", role="manager", restaurant=self.restaurant
        )
        self.waiter = User.objects.create_user(
            username="nav-waiter", role="waiter", restaurant=self.restaurant
        )
        self.chef = User.objects.create_user(
            username="nav-chef", role="chief", restaurant=self.restaurant
        )

    def test_manager_sidebar_contains_all_features_and_no_hash_links(self):
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("owner_dashboard"))
        self.assertEqual(resp.status_code, 200)

        # No broken href="#"
        self.assertNotContains(resp, 'href="#"')

        # Real links
        expected_links = [
            reverse("owner_dashboard"),
            reverse("orders_list"),
            reverse("pos_dashboard"),
            reverse("staff:daily_operations"),
            reverse("kitchen_dashboard"),
            reverse("category_list"),
            reverse("menu_item_list"),
            reverse("restaurant:floor_management"),
            reverse("restaurant:table_management"),
            reverse("inventory_category_list"),
            reverse("ingredient_list"),
            reverse("stock_count_list"),
            reverse("stock_transaction_list"),
            reverse("waste_list"),
            reverse("supplier_list"),
            reverse("purchase_list"),
            reverse("staff:employee_list"),
            reverse("staff:my_attendance"),
        ]
        for link in expected_links:
            with self.subTest(link=link):
                self.assertContains(resp, f'href="{link}"')

        # Unimplemented features show Soon badge
        self.assertContains(resp, "sidebar-badge-soon")
        self.assertContains(resp, "Soon")

    def test_waiter_sidebar_scoped_to_operational_links(self):
        self.client.force_login(self.waiter)
        resp = self.client.get(reverse("staff:daily_operations"))
        self.assertEqual(resp.status_code, 200)

        self.assertNotContains(resp, 'href="#"')

        # Waiter should see operations
        self.assertContains(resp, reverse("staff:daily_operations"))
        self.assertContains(resp, reverse("pos_dashboard"))
        self.assertContains(resp, reverse("orders_list"))
        self.assertContains(resp, reverse("restaurant:table_management"))
        self.assertContains(resp, reverse("staff:my_attendance"))
        self.assertContains(resp, reverse("staff:my_leave"))

        # Waiter should NOT see manager/back office links
        self.assertNotContains(resp, reverse("inventory_category_list"))
        self.assertNotContains(resp, reverse("ingredient_list"))
        self.assertNotContains(resp, reverse("supplier_list"))
        self.assertNotContains(resp, reverse("purchase_list"))
        self.assertNotContains(resp, reverse("category_list"))

    def test_kitchen_sidebar_scoped_to_kitchen_links(self):
        self.client.force_login(self.chef)
        resp = self.client.get(reverse("kitchen_dashboard"))
        self.assertEqual(resp.status_code, 200)

        self.assertNotContains(resp, 'href="#"')

        # Kitchen should see kitchen display and orders
        self.assertContains(resp, reverse("kitchen_dashboard"))
        self.assertContains(resp, reverse("orders_list"))
        self.assertContains(resp, reverse("staff:my_attendance"))
        self.assertContains(resp, reverse("staff:my_leave"))

        # Kitchen should NOT see POS or Back Office
        self.assertNotContains(resp, reverse("pos_dashboard"))
        self.assertNotContains(resp, reverse("ingredient_list"))
        self.assertNotContains(resp, reverse("restaurant:table_management"))

    def test_dashboard_quick_actions_and_cross_navigation(self):
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("owner_dashboard"))
        self.assertEqual(resp.status_code, 200)

        # Quick Actions are functional links
        self.assertContains(resp, f'href="{reverse("pos_dashboard")}"')
        self.assertContains(resp, f'href="{reverse("menu_item_create")}"')
        self.assertContains(resp, f'href="{reverse("ingredient_list")}"')
        self.assertContains(resp, f'href="{reverse("kitchen_dashboard")}"')

        # Tables cross-navigation
        table_resp = self.client.get(reverse("restaurant:table_management"))
        self.assertEqual(table_resp.status_code, 200)
        self.assertContains(table_resp, reverse("pos_dashboard"))
        self.assertContains(table_resp, reverse("restaurant:floor_management"))

        # Ingredient detail cross-navigation
        ing_resp = self.client.get(reverse("ingredient_detail", args=[self.chicken.pk]))
        self.assertEqual(ing_resp.status_code, 200)
        self.assertContains(ing_resp, reverse("purchase_create"))
        self.assertContains(ing_resp, reverse("waste_create"))
        self.assertContains(ing_resp, reverse("stock_count_create"))
        self.assertContains(ing_resp, reverse("stock_transaction_list"))


class BackOfficeAuthTests(TestCase):
    """
    Verifies the authentication foundation for all back-office pages.

    Rules under test:
    - Unauthenticated users must redirect to /auth/login/ for every back-office URL.
    - Any authenticated user (regardless of role) can access every implemented
      back-office page without a permission error.
    - Customer-facing / public URLs remain accessible without login.
    - base_owner.html shows the real authenticated user and Sign Out,
      never 'Staff Portal' or 'Sign In', for authenticated users.
    """

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        cls.restaurant = Restaurant.objects.create(name="Auth Test Restaurant")
        cls.dev_user = User.objects.create_user(
            username="dev_auth_test",
            password="testpass123",
            role="owner",
            restaurant=cls.restaurant,
            is_superuser=True,
            is_staff=True,
        )

    BACK_OFFICE_URL_NAMES = [
        ("owner_dashboard",             []),
        ("orders_list",                 []),
        ("kitchen_dashboard",           []),
        ("pos_dashboard",               []),
        ("menu_item_list",              []),
        ("category_list",               []),
        ("ingredient_list",             []),
        ("supplier_list",               []),
        ("purchase_list",               []),
        ("waste_list",                  []),
        ("stock_count_list",            []),
        ("stock_transaction_list",      []),
        ("restaurant:table_management", []),
        ("restaurant:floor_management", []),
        ("staff:employee_list",         []),
    ]

    def _url(self, name, args=None):
        return reverse(name, args=args or [])

    # 1. Unauthenticated → redirect to login
    def test_unauthenticated_redirected_to_login_for_every_backoffice_page(self):
        c = self.client
        for url_name, args in self.BACK_OFFICE_URL_NAMES:
            url = self._url(url_name, args)
            resp = c.get(url)
            self.assertIn(
                resp.status_code, (301, 302),
                msg=f"{url_name}: expected redirect, got HTTP {resp.status_code}",
            )
            location = resp.get("Location", "")
            self.assertIn(
                "login", location,
                msg=f"{url_name}: expected redirect to login, got Location={location!r}",
            )

    # 2. Authenticated user can access all back-office pages
    def test_authenticated_user_can_access_all_backoffice_pages(self):
        c = self.client
        c.force_login(self.dev_user)
        for url_name, args in self.BACK_OFFICE_URL_NAMES:
            url = self._url(url_name, args)
            resp = c.get(url)
            if resp.status_code == 302:
                location = resp.get("Location", "")
                self.assertNotIn(
                    "login", location,
                    msg=f"{url_name}: authenticated user redirected to login → {location}",
                )
                self.assertNotIn(
                    "landing", location,
                    msg=f"{url_name}: authenticated user redirected to landing → {location}",
                )
            else:
                self.assertEqual(
                    resp.status_code, 200,
                    msg=f"{url_name}: expected 200, got {resp.status_code}",
                )

    # 3. Customer / QR pages remain public
    def test_customer_qr_pages_are_public(self):
        from restaurant.models import Floor, Table
        floor = Floor.objects.create(
            restaurant=self.restaurant, name="Ground", floor_number=1,
        )
        table = Table.objects.create(
            restaurant=self.restaurant, floor=floor,
            table_number=1, capacity=4,
        )
        url = reverse("customer_menu", args=[self.restaurant.pk, table.pk])
        resp = self.client.get(url)  # not logged in
        self.assertNotEqual(
            resp.status_code, 302,
            msg=f"Public customer menu URL must not redirect unauthenticated users (got {resp.status_code})",
        )

    # 4. Topbar shows real user + Sign Out, never 'Staff Portal'
    def test_topbar_shows_authenticated_user_not_guest_preview(self):
        c = self.client
        c.force_login(self.dev_user)
        resp = c.get(self._url("owner_dashboard"))
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn("Sign Out", content,
                      msg="Topbar must contain 'Sign Out' for authenticated users")
        self.assertNotIn("Staff Portal", content,
                         msg="Topbar must NOT show 'Staff Portal' for authenticated users")
        self.assertNotIn(">Sign In<", content,
                         msg="Topbar must NOT show 'Sign In' for authenticated users")
        username_present = (
            self.dev_user.username in content or
            (self.dev_user.get_full_name() and self.dev_user.get_full_name() in content)
        )
        self.assertTrue(username_present,
                        msg="Topbar must display the authenticated user's name")

    def test_unauthenticated_dashboard_redirects_to_login(self):
        resp = self.client.get(self._url("owner_dashboard"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp.get("Location", ""))


@override_settings(TIME_ZONE="Asia/Dhaka")
class OwnerDashboardAnalyticsTests(TestCase):
    """Comprehensive regression tests for the upgraded Owner Dashboard analytics."""

    def setUp(self):
        from decimal import Decimal
        from django.contrib.auth import get_user_model
        from menu.models import Category, MenuItem
        from orders.models import Order, OrderItem, PaymentTransaction
        from restaurant.models import Branch, Floor, Restaurant, Table

        User = get_user_model()
        self.restaurant = Restaurant.objects.create(name="Analytics Diner")
        self.branch_a = Branch.objects.create(
            restaurant=self.restaurant, name="Branch Main", code="MAIN", is_main=True
        )
        self.branch_b = Branch.objects.create(
            restaurant=self.restaurant, name="Branch City", code="CITY"
        )

        self.floor_a = Floor.objects.create(
            restaurant=self.restaurant, branch=self.branch_a, name="Floor A", floor_number=1
        )
        self.table_a = Table.objects.create(
            restaurant=self.restaurant, branch=self.branch_a, floor=self.floor_a,
            table_number=1, capacity=4, status=Table.STATUS_AVAILABLE
        )

        self.floor_b = Floor.objects.create(
            restaurant=self.restaurant, branch=self.branch_b, name="Floor B", floor_number=1
        )
        self.table_b = Table.objects.create(
            restaurant=self.restaurant, branch=self.branch_b, floor=self.floor_b,
            table_number=2, capacity=4, status=Table.STATUS_AVAILABLE
        )

        self.owner = User.objects.create_user(
            username="dash_owner", password="pw", role="owner", restaurant=self.restaurant
        )
        self.staff_b = User.objects.create_user(
            username="dash_staff_b", password="pw", role="manager",
            restaurant=self.restaurant, branch=self.branch_b
        )

        # Fixed time: Monday, Sep 21, 2026 at 14:00 (Asia/Dhaka)
        self.now = timezone.make_aware(datetime(2026, 9, 21, 14, 0, 0))
        clock = patch("django.utils.timezone.now", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)

        # Menu items
        self.cat = Category.objects.create(restaurant=self.restaurant, name="Mains")
        self.burger = MenuItem.objects.create(
            category=self.cat, name="Special Burger",
            price=Decimal("200.00"), is_available=True
        )
        self.fries = MenuItem.objects.create(
            category=self.cat, name="French Fries",
            price=Decimal("100.00"), is_available=True
        )

        # 1. Branch A: Today Paid Order (DINE_IN)
        self.order_a_today = Order.objects.create(
            restaurant=self.restaurant, branch=self.branch_a, table=self.table_a,
            order_type="DINE_IN", status="COMPLETED", payment_status="PAID",
            subtotal=Decimal("400.00"), total_amount=Decimal("400.00"),
            paid_at=self.now, created_at=self.now,
        )
        OrderItem.objects.create(
            order=self.order_a_today, menu_item=self.burger, quantity=2, price=Decimal("200.00")
        )
        PaymentTransaction.objects.create(
            restaurant=self.restaurant, branch=self.branch_a, order=self.order_a_today,
            transaction_type="PAYMENT", amount=Decimal("400.00"), transaction_at=self.now
        )
        PaymentTransaction.objects.create(
            restaurant=self.restaurant, branch=self.branch_a, order=self.order_a_today,
            transaction_type="REFUND", amount=Decimal("50.00"), transaction_at=self.now
        )
        # Net today revenue for branch A: 400 - 50 = 350.00

        # 2. Branch A: Active Order (PREPARING, TAKEAWAY)
        self.order_a_active = Order.objects.create(
            restaurant=self.restaurant, branch=self.branch_a,
            order_type="TAKEAWAY", status="PREPARING", payment_status="UNPAID",
            subtotal=Decimal("100.00"), total_amount=Decimal("100.00"),
            created_at=self.now,
        )
        OrderItem.objects.create(
            order=self.order_a_active, menu_item=self.fries, quantity=1, price=Decimal("100.00")
        )

        # 3. Branch A: Yesterday Paid Order (Sep 20)
        yesterday_dt = self.now - timedelta(days=1)
        self.order_a_yesterday = Order.objects.create(
            restaurant=self.restaurant, branch=self.branch_a,
            order_type="TAKEAWAY", status="COMPLETED", payment_status="PAID",
            subtotal=Decimal("200.00"), total_amount=Decimal("200.00"),
            paid_at=yesterday_dt, created_at=yesterday_dt,
        )
        OrderItem.objects.create(
            order=self.order_a_yesterday, menu_item=self.burger, quantity=1, price=Decimal("200.00")
        )
        PaymentTransaction.objects.create(
            restaurant=self.restaurant, branch=self.branch_a, order=self.order_a_yesterday,
            transaction_type="PAYMENT", amount=Decimal("200.00"), transaction_at=yesterday_dt
        )

        # 4. Branch B: Today Paid Order (TAKEAWAY)
        self.order_b_today = Order.objects.create(
            restaurant=self.restaurant, branch=self.branch_b,
            order_type="TAKEAWAY", status="COMPLETED", payment_status="PAID",
            subtotal=Decimal("500.00"), total_amount=Decimal("500.00"),
            paid_at=self.now, created_at=self.now,
        )
        OrderItem.objects.create(
            order=self.order_b_today, menu_item=self.burger, quantity=2, price=Decimal("200.00")
        )
        OrderItem.objects.create(
            order=self.order_b_today, menu_item=self.fries, quantity=1, price=Decimal("100.00")
        )
        PaymentTransaction.objects.create(
            restaurant=self.restaurant, branch=self.branch_b, order=self.order_b_today,
            transaction_type="PAYMENT", amount=Decimal("500.00"), transaction_at=self.now
        )

    def _set_active_branch(self, branch):
        session = self.client.session
        session["active_branch_id"] = branch.id
        session.save()

    def test_kpi_cards_and_deltas_branch_scoped(self):
        self.client.force_login(self.owner)
        self._set_active_branch(self.branch_a)

        resp = self.client.get(reverse("owner_dashboard"))
        self.assertEqual(resp.status_code, 200)

        analytics = resp.context["analytics"]
        kpis = analytics["kpis"]

        # Today's net revenue for Branch A: 400 - 50 = 350.00
        self.assertEqual(kpis["today_revenue"], 350.00)
        # Yesterday's net revenue for Branch A: 200.00
        self.assertEqual(kpis["yesterday_revenue"], 200.00)
        # Revenue change %: ((350 - 200) / 200) * 100 = 75.0%
        self.assertEqual(kpis["revenue_change_pct"], 75.0)

        # Today's paid orders: 1
        self.assertEqual(kpis["today_orders"], 1)
        # Yesterday's paid orders: 1
        self.assertEqual(kpis["yesterday_orders"], 1)
        self.assertEqual(kpis["orders_change_pct"], 0.0)

        # Active orders: 1 (order_a_active in PREPARING)
        self.assertEqual(kpis["active_orders"], 1)

        # AOV: 350 / 1 = 350.00
        self.assertEqual(kpis["average_order_value"], 350.00)

    def test_sales_trend_periods(self):
        self.client.force_login(self.owner)
        self._set_active_branch(self.branch_a)

        # 7 Days filter
        resp = self.client.get(reverse("dashboard_analytics_api"), {"period": "7d"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        trend = data["sales_trend"]

        self.assertEqual(len(trend["labels"]), 7)
        self.assertEqual(len(trend["revenues"]), 7)
        self.assertEqual(len(trend["order_counts"]), 7)

        # Last entry is today (Sep 21): 350.00 revenue, 1 order
        self.assertEqual(trend["revenues"][-1], 350.00)
        self.assertEqual(trend["order_counts"][-1], 1)

        # Second to last is yesterday (Sep 20): 200.00 revenue, 1 order
        self.assertEqual(trend["revenues"][-2], 200.00)
        self.assertEqual(trend["order_counts"][-2], 1)

        # 30 Days filter
        resp_30 = self.client.get(reverse("dashboard_analytics_api"), {"period": "30d"})
        self.assertEqual(len(resp_30.json()["sales_trend"]["labels"]), 30)

        # This Month filter (Sep 1 to Sep 21 = 21 days)
        resp_m = self.client.get(reverse("dashboard_analytics_api"), {"period": "this_month"})
        self.assertEqual(len(resp_m.json()["sales_trend"]["labels"]), 21)

    def test_top_selling_items_and_order_types(self):
        self.client.force_login(self.owner)
        self._set_active_branch(self.branch_a)

        resp = self.client.get(reverse("dashboard_analytics_api"), {"period": "7d"})
        data = resp.json()

        # Top items for Branch A in last 7 days:
        # Special Burger: 2 today + 1 yesterday = 3 sold
        top_items = data["top_items"]
        self.assertTrue(len(top_items) >= 1)
        self.assertEqual(top_items[0]["name"], "Special Burger")
        self.assertEqual(top_items[0]["quantity"], 3)
        self.assertEqual(top_items[0]["revenue"], 600.0)

        # Sales by order type for Branch A:
        # 1 DINE_IN (400 total) + 1 TAKEAWAY (200 total) = 2 paid orders
        order_types = data["order_types"]
        self.assertEqual(order_types["dine_in"]["count"], 1)
        self.assertEqual(order_types["takeaway"]["count"], 1)
        self.assertEqual(order_types["total_count"], 2)
        self.assertEqual(order_types["dine_in"]["percentage"], 50.0)
        self.assertEqual(order_types["takeaway"]["percentage"], 50.0)

    def test_branch_isolation_and_consolidated_mode(self):
        self.client.force_login(self.owner)

        # Branch A: 350 today revenue
        self._set_active_branch(self.branch_a)
        resp_a = self.client.get(reverse("dashboard_analytics_api"))
        self.assertEqual(resp_a.json()["kpis"]["today_revenue"], 350.00)

        # Branch B: 500 today revenue
        self._set_active_branch(self.branch_b)
        resp_b = self.client.get(reverse("dashboard_analytics_api"))
        self.assertEqual(resp_b.json()["kpis"]["today_revenue"], 500.00)

        # Consolidated mode (scope=all): 350 + 500 = 850.00
        resp_all = self.client.get(reverse("dashboard_analytics_api"), {"scope": "all"})
        self.assertEqual(resp_all.json()["kpis"]["today_revenue"], 850.00)
        self.assertTrue(resp_all.json()["branch_context"]["is_consolidated"])

        # Non-owner cannot consolidate
        self.client.force_login(self.staff_b)
        resp_staff = self.client.get(reverse("dashboard_analytics_api"), {"scope": "all"})
        # Should stay scoped to staff_b assigned branch (Branch B)
        self.assertEqual(resp_staff.json()["kpis"]["today_revenue"], 500.00)
        self.assertFalse(resp_staff.json()["branch_context"]["is_consolidated"])

    def test_order_status_distribution(self):
        self.client.force_login(self.owner)
        self._set_active_branch(self.branch_a)

        resp = self.client.get(reverse("dashboard_analytics_api"))
        self.assertEqual(resp.status_code, 200)
        st = resp.json()["order_status"]

        self.assertEqual(st["PREPARING"], 1)
        self.assertEqual(st["COMPLETED"], 2)  # 1 today + 1 yesterday
        self.assertEqual(st["NEW"], 0)
        self.assertEqual(st["ACCEPTED"], 0)
        self.assertEqual(st["READY"], 0)
        self.assertEqual(st["total_active"], 1)

    def test_unauthenticated_api_redirects_to_login(self):
        resp = self.client.get(reverse("dashboard_analytics_api"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp.get("Location", ""))

    def test_empty_state_handling(self):
        from restaurant.models import Branch, Restaurant
        empty_rest = Restaurant.objects.create(name="Empty Diner")
        empty_branch = Branch.objects.create(
            restaurant=empty_rest, name="Empty Branch", code="EMP", is_main=True
        )
        from django.contrib.auth import get_user_model
        empty_owner = get_user_model().objects.create_user(
            username="empty_owner", password="pw", role="owner", restaurant=empty_rest
        )
        self.client.force_login(empty_owner)
        self._set_active_branch(empty_branch)

        resp = self.client.get(reverse("dashboard_analytics_api"))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        self.assertEqual(data["kpis"]["today_revenue"], 0.0)
        self.assertEqual(data["kpis"]["today_orders"], 0)
        self.assertEqual(data["kpis"]["active_orders"], 0)
        self.assertEqual(data["kpis"]["average_order_value"], 0.0)
        self.assertEqual(data["top_items"], [])
        self.assertEqual(data["order_types"]["total_count"], 0)
        self.assertEqual(data["sales_trend"]["revenues"], [0.0] * 7)
        self.assertEqual(data["sales_trend"]["order_counts"], [0] * 7)

    def test_default_fallback_timezone_is_asia_dhaka(self):
        from dashboard.analytics_services import get_dashboard_timezone
        from zoneinfo import ZoneInfo

        # Default fallback without configured settings must be Asia/Dhaka
        tz, tz_name = get_dashboard_timezone(self.restaurant)
        self.assertEqual(tz_name, "Asia/Dhaka")
        self.assertEqual(tz, ZoneInfo("Asia/Dhaka"))

        self.client.force_login(self.owner)
        self._set_active_branch(self.branch_a)
        resp = self.client.get(reverse("owner_dashboard"))
        self.assertEqual(resp.status_code, 200)

        # Meta contains Asia/Dhaka
        analytics = resp.context["analytics"]
        self.assertEqual(analytics["meta"]["timezone"], "Asia/Dhaka")

        # In self.now (14:00 Asia/Dhaka), formatted time is 02:00:00 PM
        self.assertEqual(analytics["meta"]["last_refreshed"], "02:00:00 PM")
        self.assertContains(resp, "Live • 02:00:00 PM")

    def test_configured_restaurant_and_branch_timezones_and_date_boundaries(self):
        from business_settings.models import BranchSettings, RestaurantSettings
        from decimal import Decimal
        from orders.models import Order, PaymentTransaction
        from zoneinfo import ZoneInfo

        # Configure restaurant timezone to America/New_York (EDT, UTC-4 in September)
        RestaurantSettings.objects.create(
            restaurant=self.restaurant,
            values={"timezone": "America/New_York"}
        )

        # Let now be 2026-09-22 01:30:00 UTC
        # In UTC: Sep 22
        # In Dhaka (UTC+6): 07:30:00 on Sep 22
        # In New York (UTC-4): 21:30:00 on Sep 21 (TODAY in New York!)
        fixed_dt = timezone.make_aware(datetime(2026, 9, 22, 1, 30, 0), ZoneInfo("UTC"))

        with patch("django.utils.timezone.now", return_value=fixed_dt):
            # Create an order paid at 01:15 UTC on Sep 22
            order_ny = Order.objects.create(
                restaurant=self.restaurant, branch=self.branch_a,
                order_type="TAKEAWAY", status="COMPLETED", payment_status="PAID",
                subtotal=Decimal("800.00"), total_amount=Decimal("800.00"),
                paid_at=fixed_dt, created_at=fixed_dt,
            )
            PaymentTransaction.objects.create(
                restaurant=self.restaurant, branch=self.branch_a, order=order_ny,
                transaction_type="PAYMENT", amount=Decimal("800.00"), transaction_at=fixed_dt
            )

            self.client.force_login(self.owner)
            self._set_active_branch(self.branch_a)
            resp = self.client.get(reverse("dashboard_analytics_api"))
            self.assertEqual(resp.status_code, 200)
            data = resp.json()

            # Timezone is America/New_York
            self.assertEqual(data["meta"]["timezone"], "America/New_York")
            # Today in New York is Sep 21, 2026
            self.assertEqual(data["meta"]["today_date"], "Sep 21, 2026")
            # 01:30 UTC is 09:30:00 PM in New York
            self.assertEqual(data["meta"]["last_refreshed"], "09:30:00 PM")

            # The order created at 01:15 UTC Sep 22 counts as TODAY in New York
            # (along with earlier order_a_today 350.00) -> 800 + 350 = 1150.00
            self.assertEqual(data["kpis"]["today_revenue"], 1150.00)

            # Test branch override: Branch B configured to Asia/Tokyo (UTC+9)
            BranchSettings.objects.create(
                branch=self.branch_b,
                values={"timezone": "Asia/Tokyo"}
            )
            self._set_active_branch(self.branch_b)
            resp_b = self.client.get(reverse("dashboard_analytics_api"))
            self.assertEqual(resp_b.json()["meta"]["timezone"], "Asia/Tokyo")
            # In Tokyo (UTC+9), 01:30 UTC on Sep 22 is 10:30 AM on Sep 22
            self.assertEqual(resp_b.json()["meta"]["today_date"], "Sep 22, 2026")

            # In consolidated mode (scope=all), owner uses restaurant timezone America/New_York
            resp_all = self.client.get(reverse("dashboard_analytics_api"), {"scope": "all"})
            self.assertEqual(resp_all.json()["meta"]["timezone"], "America/New_York")
            self.assertEqual(resp_all.json()["meta"]["today_date"], "Sep 21, 2026")



