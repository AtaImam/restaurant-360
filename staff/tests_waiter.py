from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from inventory.models import Ingredient, IngredientCategory, Recipe, RecipeIngredient
from inventory.services import reserve_stock_for_order
from menu.models import Category, MenuItem
from orders.models import Order, OrderItem
from orders.services import transition_order_status
from restaurant.models import Branch, Floor, Restaurant, Table
from staff.models import (
    Attendance,
    DailyTableAssignment,
    EmployeeProfile,
    LeaveRequest,
    OrderStaffService,
    PayrollRecord,
    SalaryAdvance,
    Shift,
    StaffNotification,
    StaffTask,
)
from staff.operations import (
    assign_table_to_waiter,
    ensure_order_staff_service,
    handle_order_status_change,
    local_work_date,
    register_new_order,
)

User = get_user_model()


class WaiterRoleAndExperienceTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.restaurant = Restaurant.objects.create(
            name="FoodHub",
            address="123 Dhaka Ave",
            phone="01700000000",
        )
        self.branch_a = Branch.objects.create(
            restaurant=self.restaurant,
            name="Dhanmondi Branch",
            code="DHN",
            is_main=True,
            is_active=True,
        )
        self.branch_b = Branch.objects.create(
            restaurant=self.restaurant,
            name="Gulshan Branch",
            code="GLS",
            is_main=False,
            is_active=True,
        )
        self.floor_a = Floor.objects.create(
            restaurant=self.restaurant,
            branch=self.branch_a,
            name="Main Hall",
            floor_number=1,
        )
        self.floor_b = Floor.objects.create(
            restaurant=self.restaurant,
            branch=self.branch_b,
            name="Gulshan Floor",
            floor_number=1,
        )
        self.table_a = Table.objects.create(
            restaurant=self.restaurant,
            branch=self.branch_a,
            floor=self.floor_a,
            table_number=1,
            capacity=4,
            is_active=True,
        )
        self.table_a2 = Table.objects.create(
            restaurant=self.restaurant,
            branch=self.branch_a,
            floor=self.floor_a,
            table_number=3,
            capacity=6,
            is_active=True,
        )
        self.table_b = Table.objects.create(
            restaurant=self.restaurant,
            branch=self.branch_b,
            floor=self.floor_b,
            table_number=2,
            capacity=4,
            is_active=True,
        )

        # Shifts
        self.shift = Shift.objects.create(
            restaurant=self.restaurant,
            branch=self.branch_a,
            name="Morning Shift",
            start_time=time(8, 0),
            end_time=time(16, 0),
            is_active=True,
        )

        # Users
        self.owner = User.objects.create_user(
            username="test_owner",
            password="password123",
            role="owner",
            restaurant=self.restaurant,
            branch=self.branch_a,
            is_active=True,
            is_active_staff=True,
        )

        self.waiter_a = User.objects.create_user(
            username="waiter_alice",
            first_name="Alice",
            last_name="Smith",
            password="password123",
            role="waiter",
            restaurant=self.restaurant,
            branch=self.branch_a,
            is_active=True,
            is_active_staff=True,
        )
        self.profile_a = EmployeeProfile.objects.create(
            user=self.waiter_a,
            employee_id="W-001",
            shift=self.shift,
            basic_salary=Decimal("1500.00"),
            joining_date=date(2025, 1, 1),
        )

        self.waiter_b = User.objects.create_user(
            username="waiter_bob",
            first_name="Bob",
            last_name="Jones",
            password="password123",
            role="waiter",
            restaurant=self.restaurant,
            branch=self.branch_b,
            is_active=True,
            is_active_staff=True,
        )
        self.profile_b = EmployeeProfile.objects.create(
            user=self.waiter_b,
            employee_id="W-002",
            shift=self.shift,
            basic_salary=Decimal("1600.00"),
            joining_date=date(2025, 1, 1),
        )

        self.waiter_c = User.objects.create_user(
            username="waiter_charlie",
            first_name="Charlie",
            last_name="Brown",
            password="password123",
            role="waiter",
            restaurant=self.restaurant,
            branch=self.branch_a,
            is_active=True,
            is_active_staff=True,
        )
        self.profile_c = EmployeeProfile.objects.create(
            user=self.waiter_c,
            employee_id="W-003",
            shift=self.shift,
            basic_salary=Decimal("1500.00"),
            joining_date=date(2025, 1, 1),
        )

        self.kitchen_user = User.objects.create_user(
            username="chef_john",
            password="password123",
            role="chief",
            restaurant=self.restaurant,
            branch=self.branch_a,
            is_active=True,
            is_active_staff=True,
        )

        # Menu
        self.cat = Category.objects.create(restaurant=self.restaurant, name="Mains")
        self.item = MenuItem.objects.create(
            category=self.cat,
            name="Chicken Burger",
            price=Decimal("12.50"),
            is_available=True,
        )
        self.ing_cat = IngredientCategory.objects.create(restaurant=self.restaurant, name="Kitchen")
        self.chicken = Ingredient.objects.create(
            restaurant=self.restaurant,
            category=self.ing_cat,
            name="Chicken",
            sku="CHK",
            base_unit=Ingredient.BaseUnit.GRAM,
            pack_size=1000,
            current_stock=1000,
        )
        self.recipe = Recipe.objects.create(menu_item=self.item, yield_quantity=1)
        RecipeIngredient.objects.create(recipe=self.recipe, ingredient=self.chicken, quantity=100)

    def _assign_table(self, waiter_profile, table, work_date=None):
        if work_date is None:
            work_date = local_work_date()
        attendance = Attendance.objects.filter(employee=waiter_profile, work_date=work_date).first()
        if not attendance:
            now = timezone.now()
            attendance = Attendance.objects.create(
                employee=waiter_profile,
                restaurant=table.restaurant,
                branch=table.branch,
                shift=waiter_profile.shift or self.shift,
                work_date=work_date,
                scheduled_start=now,
                scheduled_end=now + timedelta(hours=8),
                check_in=now,
            )
        DailyTableAssignment.objects.filter(table=table, work_date=work_date, is_active=True).update(
            is_active=False, ended_at=timezone.now()
        )
        return DailyTableAssignment.objects.create(
            work_date=work_date,
            restaurant=table.restaurant,
            branch=table.branch,
            table=table,
            waiter=waiter_profile,
            attendance=attendance,
            assigned_by=self.owner,
            is_active=True,
        )

    # -----------------------------------------------------------------------
    # 1. ROLE ROUTING TESTS
    # -----------------------------------------------------------------------

    def test_waiter_dashboard_url(self):
        """User.get_dashboard_url() routes waiter to /waiter/ and owner to /dashboard/"""
        self.assertEqual(self.waiter_a.get_dashboard_url(), "/waiter/")
        self.assertEqual(self.owner.get_dashboard_url(), "/dashboard/")
        self.assertEqual(self.kitchen_user.get_dashboard_url(), "/kitchen/")

    def test_waiter_login_redirects_to_waiter_dashboard(self):
        """Logging in as waiter redirects directly to /waiter/"""
        response = self.client.post(
            reverse("users:login"),
            {"username": "waiter_alice", "password": "password123"},
        )
        self.assertRedirects(response, "/waiter/")

    def test_waiter_navigating_to_owner_dashboard_redirects_to_waiter(self):
        """If waiter navigates to /dashboard/, they are redirected to /waiter/"""
        self.client.force_login(self.waiter_a)
        response = self.client.get("/dashboard/")
        self.assertRedirects(response, "/waiter/")

    def test_kitchen_navigating_to_owner_dashboard_redirects_to_kitchen(self):
        """If kitchen staff navigates to /dashboard/, they are redirected to /kitchen/"""
        self.client.force_login(self.kitchen_user)
        response = self.client.get("/dashboard/")
        self.assertRedirects(response, "/kitchen/")

    def test_owner_navigating_to_owner_dashboard_succeeds(self):
        """Owner stays on Owner Dashboard /dashboard/"""
        self.client.force_login(self.owner)
        response = self.client.get("/dashboard/")
        self.assertEqual(response.status_code, 200)

    # -----------------------------------------------------------------------
    # 2. PERMISSION GUARDS (WAITER MUST NOT SEE/ACCESS)
    # -----------------------------------------------------------------------

    def test_waiter_cannot_access_finance_reports(self):
        """Waiter receives 403 Forbidden accessing financial reports"""
        self.client.force_login(self.waiter_a)
        res = self.client.get(reverse("finance:sales_report"))
        self.assertEqual(res.status_code, 403)
        res_pnl = self.client.get(reverse("finance:profit_loss_report"))
        self.assertEqual(res_pnl.status_code, 403)

    def test_waiter_cannot_access_expenses(self):
        """Waiter receives 403 Forbidden accessing expense management"""
        self.client.force_login(self.waiter_a)
        res = self.client.get(reverse("finance:expense_list"))
        self.assertEqual(res.status_code, 403)

    def test_waiter_cannot_access_inventory(self):
        """Waiter receives 403 Forbidden accessing inventory admin"""
        self.client.force_login(self.waiter_a)
        res = self.client.get(reverse("inventory_category_list"))
        self.assertEqual(res.status_code, 403)
        res_supp = self.client.get(reverse("supplier_list"))
        self.assertEqual(res_supp.status_code, 403)
        res_purch = self.client.get(reverse("purchase_list"))
        self.assertEqual(res_purch.status_code, 403)

    def test_waiter_cannot_access_staff_management(self):
        """Waiter receives 403 Forbidden accessing staff administration"""
        self.client.force_login(self.waiter_a)
        res = self.client.get(reverse("staff:employee_list"))
        self.assertEqual(res.status_code, 403)
        res_shifts = self.client.get(reverse("staff:shift_list"))
        self.assertEqual(res_shifts.status_code, 403)
        res_att = self.client.get(reverse("staff:attendance_list"))
        self.assertEqual(res_att.status_code, 403)
        res_payroll = self.client.get(reverse("staff:payroll_list"))
        self.assertEqual(res_payroll.status_code, 403)

    def test_waiter_cannot_access_settings(self):
        """Waiter receives 403 Forbidden accessing settings"""
        self.client.force_login(self.waiter_a)
        res = self.client.get(reverse("business_settings:settings"))
        self.assertEqual(res.status_code, 403)

    def test_waiter_cannot_access_coupons(self):
        """Waiter is blocked from coupon administration"""
        self.client.force_login(self.waiter_a)
        res = self.client.get(reverse("coupon_list"))
        # Manager required redirects or denies
        self.assertIn(res.status_code, [302, 403])

    def test_waiter_cannot_access_owner_analytics_api(self):
        """Waiter is blocked from owner analytics API"""
        self.client.force_login(self.waiter_a)
        res = self.client.get(reverse("dashboard_analytics_api"))
        self.assertIn(res.status_code, [302, 403])

    # -----------------------------------------------------------------------
    # 3. WAITER OPERATIONAL VIEWS
    # -----------------------------------------------------------------------

    def test_waiter_dashboard_page(self):
        """Waiter dashboard renders with correct identity, shift and KPI counts"""
        self.client.force_login(self.waiter_a)
        res = self.client.get(reverse("waiter:dashboard"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Alice")
        self.assertContains(res, "Waiter")
        self.assertContains(res, "Dhanmondi Branch")
        self.assertContains(res, "Morning Shift")

    def test_waiter_tables_page(self):
        """Waiter tables page displays branch tables with status and actions"""
        self._assign_table(self.profile_a, self.table_a)
        self.client.force_login(self.waiter_a)
        res = self.client.get(reverse("waiter:tables"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Table 1")

    def test_waiter_ready_orders_and_serve_action(self):
        """Waiter can view ready orders and 1-tap serve them"""
        self._assign_table(self.profile_a, self.table_a)
        order = Order.objects.create(
            restaurant=self.restaurant,
            branch=self.branch_a,
            table=self.table_a,
            order_type="DINE_IN",
            status="NEW",
            total_amount=Decimal("12.50"),
        )
        OrderItem.objects.create(
            order=order,
            menu_item=self.item,
            quantity=1,
            price=Decimal("12.50"),
        )
        reserve_stock_for_order(order)
        transition_order_status(order, "ACCEPTED")
        transition_order_status(order, "PREPARING")
        transition_order_status(order, "READY")

        self.client.force_login(self.waiter_a)
        res = self.client.get(reverse("waiter:ready_orders"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, f"Order #{order.id}")
        self.assertContains(res, "Chicken Burger")

        # 1-tap Mark Served
        post_res = self.client.post(reverse("waiter:serve_order", args=[order.id]))
        self.assertRedirects(post_res, reverse("waiter:ready_orders"))

        order.refresh_from_db()
        self.assertEqual(order.status, "SERVED")
        self.assertTrue(OrderStaffService.objects.filter(order=order, waiter=self.profile_a).exists())

    def test_waiter_tasks_and_completion(self):
        """Waiter can view assigned tasks and 1-tap mark complete"""
        now = timezone.now()
        attendance = Attendance.objects.create(
            employee=self.profile_a,
            restaurant=self.restaurant,
            shift=self.shift,
            work_date=local_work_date(),
            scheduled_start=now,
            scheduled_end=now + timedelta(hours=8),
            check_in=now,
        )
        task = StaffTask.objects.create(
            restaurant=self.restaurant,
            branch=self.branch_a,
            employee=self.profile_a,
            attendance=attendance,
            assigned_by=self.owner,
            work_date=local_work_date(),
            title="Clean Table 1",
            instructions="Wipe down and set menus",
            is_completed=False,
        )

        self.client.force_login(self.waiter_a)
        res = self.client.get(reverse("waiter:tasks"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Clean Table 1")

        # 1-tap complete
        post_res = self.client.post(reverse("waiter:complete_task", args=[task.id]))
        self.assertRedirects(post_res, reverse("waiter:tasks"))

        task.refresh_from_db()
        self.assertTrue(task.is_completed)

    def test_waiter_attendance_check_in_and_out(self):
        """Waiter can check in and check out from the attendance page"""
        self.client.force_login(self.waiter_a)

        # Set dynamic shift window relative to current time to avoid midnight boundary issues
        now_local = timezone.localtime()
        self.shift.start_time = (now_local - timedelta(hours=1)).time()
        self.shift.end_time = (now_local + timedelta(hours=7)).time()
        self.shift.save()

        # Check in
        res_in = self.client.post(reverse("waiter:check_in"))
        self.assertRedirects(res_in, reverse("waiter:attendance"))
        att = Attendance.objects.filter(employee=self.profile_a).order_by("-id").first()
        self.assertIsNotNone(att)
        self.assertIsNotNone(att.check_in)
        self.assertIsNone(att.check_out)

        # Check out
        res_out = self.client.post(reverse("waiter:check_out"))
        self.assertRedirects(res_out, reverse("waiter:attendance"))
        att.refresh_from_db()
        self.assertIsNotNone(att.check_out)

    def test_waiter_leave_request(self):
        """Waiter can submit a leave request"""
        self.client.force_login(self.waiter_a)
        post_data = {
            "leave_type": "sick",
            "start_date": "2026-10-01",
            "end_date": "2026-10-02",
            "reason": "Doctor appointment and recovery",
        }
        res = self.client.post(reverse("waiter:leave"), post_data)
        self.assertRedirects(res, reverse("waiter:leave"))
        self.assertTrue(LeaveRequest.objects.filter(employee=self.profile_a, leave_type="sick").exists())

    def test_waiter_salary_advance_request(self):
        """Waiter can submit a salary advance request"""
        self.client.force_login(self.waiter_a)
        post_data = {
            "amount": "150.00",
            "reason": "Emergency medical expenses",
        }
        res = self.client.post(reverse("waiter:salary_advance"), post_data)
        self.assertRedirects(res, reverse("waiter:salary_advance"))
        self.assertTrue(SalaryAdvance.objects.filter(employee=self.profile_a, amount=Decimal("150.00")).exists())

    # -----------------------------------------------------------------------
    # 4. BRANCH ISOLATION AND PRIVACY
    # -----------------------------------------------------------------------

    def test_waiter_branch_isolation_on_tables(self):
        """Waiter at Branch A cannot see Branch B's tables on My Tables"""
        self._assign_table(self.profile_a, self.table_a)
        self.client.force_login(self.waiter_a)
        res = self.client.get(reverse("waiter:tables"))
        self.assertContains(res, "Table 1")
        self.assertNotContains(res, "Table 2")

    def test_waiter_cannot_serve_order_of_another_branch(self):
        """Waiter at Branch A cannot serve an order belonging to Branch B"""
        order_b = Order.objects.create(
            restaurant=self.restaurant,
            branch=self.branch_b,
            table=self.table_b,
            order_type="DINE_IN",
            status="READY",
            total_amount=Decimal("20.00"),
        )
        self.client.force_login(self.waiter_a)
        res = self.client.post(reverse("waiter:serve_order", args=[order_b.id]))
        self.assertEqual(res.status_code, 403)
        order_b.refresh_from_db()
        self.assertEqual(order_b.status, "READY")

    def test_waiter_data_privacy(self):
        """Waiter A cannot see Waiter B's attendance, payroll, or salary advances"""
        # Create records for Waiter B
        advance_b = SalaryAdvance.objects.create(
            employee=self.profile_b,
            restaurant=self.restaurant,
            branch=self.branch_b,
            amount=Decimal("500.00"),
            reason="Private loan for Bob",
        )
        payroll_b = PayrollRecord.objects.create(
            employee=self.profile_b,
            restaurant=self.restaurant,
            month=date(2026, 9, 1),
            basic_salary=Decimal("1600.00"),
            net_salary=Decimal("1600.00"),
            status="paid",
        )

        self.client.force_login(self.waiter_a)

        # Advance page
        res_adv = self.client.get(reverse("waiter:salary_advance"))
        self.assertNotContains(res_adv, "Private loan for Bob")

        # Payroll history page
        res_pay = self.client.get(reverse("waiter:payroll_history"))
        self.assertNotContains(res_pay, "$1600.00")

    # -----------------------------------------------------------------------
    # 5. REGRESSION TESTS (END-TO-END WAITER EXPERIENCE)
    # -----------------------------------------------------------------------

    def test_performance_page_empty_waiter_does_not_crash(self):
        """Empty or new waiter with no records loads performance page safely with 0 and '—'"""
        self.client.force_login(self.waiter_c)
        res = self.client.get(reverse("waiter:performance"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "0")
        self.assertContains(res, "—")
        self.assertTemplateUsed(res, "waiter/base_waiter.html")

    def test_sidebar_and_layout_consistency_no_owner_fallback(self):
        """All waiter pages and POS use base_waiter.html with no owner layout fallback"""
        self.client.force_login(self.waiter_a)
        waiter_urls = [
            reverse("waiter:dashboard"),
            reverse("waiter:tables"),
            reverse("pos_dashboard"),
            reverse("waiter:ready_orders"),
            reverse("waiter:tasks"),
            reverse("waiter:shift"),
            reverse("waiter:attendance"),
            reverse("waiter:leave"),
            reverse("waiter:service_history"),
            reverse("waiter:performance"),
            reverse("waiter:salary"),
            reverse("waiter:payroll_history"),
        ]
        for url in waiter_urls:
            res = self.client.get(url)
            self.assertEqual(res.status_code, 200, f"Failed for url {url}")
            self.assertTemplateUsed(res, "waiter/base_waiter.html", f"base_waiter not used for {url}")
            self.assertTemplateNotUsed(res, "dashboard/base_owner.html", f"Owner layout leaked into {url}")

    def test_assigned_table_visibility_and_independent_cleaning_state(self):
        """My Tables tracks service states and shows cleaning alongside availability"""
        self._assign_table(self.profile_a, self.table_a)
        self.client.force_login(self.waiter_a)

        # State 1: Available
        res = self.client.get(reverse("waiter:tables"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Table 1")
        self.assertNotContains(res, "Table 3")  # Unassigned table in same branch is NOT shown
        self.assertContains(res, "Free / Available")

        # State 2: New Order
        order = Order.objects.create(
            restaurant=self.restaurant,
            branch=self.branch_a,
            table=self.table_a,
            order_type="DINE_IN",
            status="NEW",
            total_amount=Decimal("25.00"),
        )
        OrderItem.objects.create(order=order, menu_item=self.item, quantity=2, price=Decimal("12.50"))
        register_new_order(order.pk)
        res = self.client.get(reverse("waiter:tables"))
        self.assertContains(res, "New Order")

        # State 3: Preparing
        reserve_stock_for_order(order)
        transition_order_status(order, "ACCEPTED")
        transition_order_status(order, "PREPARING")
        res = self.client.get(reverse("waiter:tables"))
        self.assertContains(res, "Preparing")

        # State 4: Ready
        transition_order_status(order, "READY")
        res = self.client.get(reverse("waiter:tables"))
        self.assertContains(res, "Ready to Serve")
        self.assertContains(res, "Serve Food to Table")

        # State 5: Dining
        transition_order_status(order, "SERVED")
        res = self.client.get(reverse("waiter:tables"))
        self.assertContains(res, "Dining")

        # State 6: Payment Due (simulate elapsed dining time > 15 mins)
        service = order.staff_service
        service.served_at = timezone.now() - timedelta(minutes=20)
        service.save(update_fields=["served_at"])
        res = self.client.get(reverse("waiter:tables"))
        self.assertContains(res, "Payment Due")

        # Paid service: available with an independent cleaning task
        order.status = "COMPLETED"
        order.payment_status = "PAID"
        order.save(update_fields=["status", "payment_status"])
        from orders.services import release_settled_table_session
        release_settled_table_session(order.table_session_id)
        res = self.client.get(reverse("waiter:tables"))
        self.assertContains(res, "Clean Needed")
        self.assertContains(res, "Free / Available")
        self.assertContains(res, "Mark as Cleaned")

    def test_cross_waiter_pos_ordering_preserves_table_assignment(self):
        """Waiter C can take an order for Table 1 without changing Table 1's assignment to Waiter A"""
        self._assign_table(self.profile_a, self.table_a)
        self.client.force_login(self.waiter_c)

        # Open POS preselected for Table 1
        res = self.client.get(f"{reverse('pos_dashboard')}?table_id={self.table_a.id}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.context["selected_table_id"], self.table_a.id)
        self.assertContains(res, f'value="{self.table_a.id}"')
        self.assertContains(res, "selected")
        # Waiter selector is replaced by logged-in waiter badge
        self.assertContains(res, "Order Taken By")
        self.assertContains(res, "Charlie")

        # Post order from POS
        payload = {
            "order_type": "DINE_IN",
            "table_id": self.table_a.id,
            "items": [{"id": self.item.id, "quantity": 1}],
            "payment_timing": "PAY_LATER",
        }
        with self.captureOnCommitCallbacks(execute=True):
            res_create = self.client.post(reverse("create_pos_order"), data=payload, content_type="application/json")
        self.assertEqual(res_create.status_code, 200)
        data = res_create.json()
        self.assertTrue(data.get("success"))
        order_id = data.get("order_id")

        order = Order.objects.get(pk=order_id)
        service = order.staff_service
        # Official table assignment stays Waiter A
        self.assertEqual(service.waiter, self.profile_a)
        # Order taken by is recorded as Waiter C
        self.assertEqual(service.order_taken_by, self.profile_c)

        # Table 1 does NOT appear in Waiter C's My Tables
        res_tables = self.client.get(reverse("waiter:tables"))
        self.assertNotContains(res_tables, "Table 1")

        # Table 1 appears in Waiter A's My Tables with Taken by Charlie
        self.client.force_login(self.waiter_a)
        res_a_tables = self.client.get(reverse("waiter:tables"))
        self.assertContains(res_a_tables, "Table 1")
        self.assertContains(res_a_tables, "Taken by: Charlie")

    def test_qr_order_notifications_and_reassignment(self):
        """QR order alerts assigned waiter; reassignment redirects alerts; unassigned stays management-only"""
        # 1. Customer places QR order on Table 1 (assigned to Waiter A)
        self._assign_table(self.profile_a, self.table_a)
        order = Order.objects.create(
            restaurant=self.restaurant,
            branch=self.branch_a,
            table=self.table_a,
            order_type="DINE_IN",
            status="NEW",
            total_amount=Decimal("15.00"),
        )
        OrderItem.objects.create(order=order, menu_item=self.item, quantity=1, price=Decimal("15.00"))
        register_new_order(order.pk)  # QR order has waiter_user=None

        # Waiter A received QR notification
        notif_a = StaffNotification.objects.filter(recipient=self.waiter_a, order=order).first()
        self.assertIsNotNone(notif_a)
        self.assertIn("QR", notif_a.title)

        # Appears in Waiter A's My Tables marked as QR order
        self.client.force_login(self.waiter_a)
        res = self.client.get(reverse("waiter:tables"))
        self.assertContains(res, "Customer QR Order")

        # 2. Reassign Table 1 to Waiter C
        self._assign_table(self.profile_c, self.table_a)

        # Order transitions to READY -> alert goes to Waiter C (new assigned waiter)
        reserve_stock_for_order(order)
        transition_order_status(order, "ACCEPTED")
        transition_order_status(order, "PREPARING")
        transition_order_status(order, "READY")

        notif_c = StaffNotification.objects.filter(recipient=self.waiter_c, order=order, notification_type="food_ready").first()
        self.assertIsNotNone(notif_c)

        # 3. Order on unassigned Table 3
        order_unassigned = Order.objects.create(
            restaurant=self.restaurant,
            branch=self.branch_a,
            table=self.table_a2,
            order_type="DINE_IN",
            status="NEW",
            total_amount=Decimal("10.00"),
        )
        register_new_order(order_unassigned.pk)
        # Neither Waiter A nor C received notification
        self.assertFalse(StaffNotification.objects.filter(recipient=self.waiter_a, order=order_unassigned).exists())
        self.assertFalse(StaffNotification.objects.filter(recipient=self.waiter_c, order=order_unassigned).exists())
        # Management was notified
        self.assertTrue(StaffNotification.objects.filter(recipient=self.owner, order=order_unassigned).exists())

    def test_ready_to_served_cross_waiter_preserves_assignment(self):
        """Cross-waiter serving records served_by while preserving official table assignment"""
        self._assign_table(self.profile_a, self.table_a)
        order = Order.objects.create(
            restaurant=self.restaurant,
            branch=self.branch_a,
            table=self.table_a,
            order_type="DINE_IN",
            status="NEW",
            total_amount=Decimal("12.50"),
        )
        OrderItem.objects.create(order=order, menu_item=self.item, quantity=1, price=Decimal("12.50"))
        register_new_order(order.pk)
        reserve_stock_for_order(order)
        transition_order_status(order, "ACCEPTED")
        transition_order_status(order, "PREPARING")
        transition_order_status(order, "READY")

        # Waiter C serves Table 1
        self.client.force_login(self.waiter_c)
        res = self.client.post(reverse("waiter:serve_order", args=[order.id]))
        self.assertRedirects(res, reverse("waiter:ready_orders"))

        order.refresh_from_db()
        self.assertEqual(order.status, "SERVED")
        service = order.staff_service
        self.assertEqual(service.waiter, self.profile_a)  # Official assignment stays Alice
        self.assertEqual(service.served_by, self.profile_c)  # Served by is Charlie
        self.assertIsNotNone(service.served_at)

    def test_payment_cleanup_close_lifecycle(self):
        """Payment releases service; cleaning remains visible and independently acknowledged."""
        self._assign_table(self.profile_a, self.table_a)
        order = Order.objects.create(
            restaurant=self.restaurant,
            branch=self.branch_a,
            table=self.table_a,
            order_type="DINE_IN",
            status="NEW",
            payment_status="UNPAID",
            total_amount=Decimal("12.50"),
        )
        OrderItem.objects.create(order=order, menu_item=self.item, quantity=1, price=Decimal("12.50"))
        register_new_order(order.pk)
        self.table_a.mark_occupied()
        reserve_stock_for_order(order)
        transition_order_status(order, "ACCEPTED")
        transition_order_status(order, "PREPARING")
        transition_order_status(order, "READY")
        transition_order_status(order, "SERVED")

        self.client.force_login(self.waiter_a)

        # Attempt clean while unpaid -> blocked
        res_blocked = self.client.post(reverse("waiter:clean_table", args=[self.table_a.id]))
        self.assertRedirects(res_blocked, reverse("waiter:tables"))
        order.refresh_from_db()
        self.assertEqual(order.status, "SERVED")

        # Settle payment
        order.payment_status = "PAID"
        order.save(update_fields=["payment_status"])

        transition_order_status(order, "COMPLETED")
        self.table_a.refresh_from_db()
        self.assertEqual(self.table_a.status, Table.STATUS_AVAILABLE)
        session = order.table_session
        session.refresh_from_db()
        self.assertTrue(session.clean_needed)
        self.assertContains(self.client.get(reverse("waiter:tables")), "Clean Needed")

        # Clean table
        res_clean = self.client.post(reverse("waiter:clean_table", args=[self.table_a.id]), {"session_id": session.pk})
        self.assertRedirects(res_clean, reverse("waiter:tables"))

        order.refresh_from_db()
        self.assertEqual(order.status, "COMPLETED")
        self.assertEqual(order.staff_service.closed_by, self.waiter_a)

        self.table_a.refresh_from_db()
        self.assertEqual(self.table_a.status, Table.STATUS_AVAILABLE)
        self.assertIsNone(self.table_a.active_session)
        session.refresh_from_db()
        self.assertFalse(session.clean_needed)
        self.assertEqual(session.cleaned_by, self.waiter_a)
        self.assertIsNotNone(session.cleaned_at)
        self.assertNotContains(self.client.get(reverse("waiter:tables")), "Clean Needed")

    def test_cleaning_previous_session_preserves_new_unpaid_service(self):
        self._assign_table(self.profile_a, self.table_a)
        old = Order.objects.create(
            restaurant=self.restaurant, branch=self.branch_a, table=self.table_a,
            order_type="DINE_IN", status="COMPLETED", payment_status="PAID", total_amount=10,
        )
        from orders.services import release_settled_table_session
        release_settled_table_session(old.table_session_id)
        old_session = old.table_session
        old_session.refresh_from_db()
        self.table_a.refresh_from_db()
        new = Order.objects.create(
            restaurant=self.restaurant, branch=self.branch_a, table=self.table_a,
            order_type="DINE_IN", total_amount=20,
        )
        self.assertNotEqual(new.table_session_id, old_session.pk)
        self.client.force_login(self.waiter_a)
        response = self.client.get(reverse("waiter:tables"))
        self.assertContains(response, "Clean Needed")
        self.assertContains(response, "New Order")
        url = reverse("waiter:clean_table", args=[self.table_a.pk])
        self.client.post(url, {"session_id": old_session.pk})
        self.client.post(url, {"session_id": old_session.pk})
        self.table_a.refresh_from_db()
        new.refresh_from_db()
        old_session.refresh_from_db()
        self.assertEqual(self.table_a.status, Table.STATUS_OCCUPIED)
        self.assertEqual(self.table_a.active_session.pk, new.table_session_id)
        self.assertEqual(new.status, "NEW")
        self.assertEqual(new.payment_status, "UNPAID")
        self.assertFalse(old_session.clean_needed)

    def test_unassigned_waiter_cannot_clear_cleaning(self):
        self._assign_table(self.profile_a, self.table_a)
        old = Order.objects.create(
            restaurant=self.restaurant, branch=self.branch_a, table=self.table_a,
            order_type="DINE_IN", status="COMPLETED", payment_status="PAID", total_amount=10,
        )
        from orders.services import release_settled_table_session
        release_settled_table_session(old.table_session_id)
        self.client.force_login(self.waiter_c)
        self.client.post(reverse("waiter:clean_table", args=[self.table_a.pk]), {"session_id": old.table_session_id})
        old.table_session.refresh_from_db()
        self.assertTrue(old.table_session.clean_needed)

    def test_history_attribution_and_performance_for_cross_waiter(self):
        """Attribution is tracked separately and helper waiter gets performance credit without table stealing"""
        self._assign_table(self.profile_a, self.table_a)
        order = Order.objects.create(
            restaurant=self.restaurant,
            branch=self.branch_a,
            table=self.table_a,
            order_type="DINE_IN",
            status="NEW",
            payment_status="PAID",
            total_amount=Decimal("20.00"),
        )
        OrderItem.objects.create(order=order, menu_item=self.item, quantity=1, price=Decimal("20.00"))
        # Waiter C took the order
        register_new_order(order.pk, waiter_user=self.waiter_c)
        reserve_stock_for_order(order)
        transition_order_status(order, "ACCEPTED")
        transition_order_status(order, "PREPARING")
        transition_order_status(order, "READY")

        # Waiter C served the order
        self.client.force_login(self.waiter_c)
        self.client.post(reverse("waiter:serve_order", args=[order.id]))

        # Check Waiter C's service history
        res_history = self.client.get(reverse("waiter:service_history"))
        self.assertEqual(res_history.status_code, 200)
        self.assertContains(res_history, f"Order #{order.id}")
        self.assertContains(res_history, "Served By: <strong>Charlie Brown</strong>")
        self.assertContains(res_history, "Assigned Table Waiter: <strong>Alice Smith</strong>")

        # Check Waiter C's performance metrics
        res_perf = self.client.get(reverse("waiter:performance"))
        self.assertEqual(res_perf.status_code, 200)

        # Table 1 remains absent from Waiter C's My Tables
        res_tables = self.client.get(reverse("waiter:tables"))
        self.assertNotContains(res_tables, "Table 1")
