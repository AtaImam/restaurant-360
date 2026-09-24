import calendar
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from menu.models import Category, MenuItem
from orders.models import Order, OrderItem, TableSession
from restaurant.models import Restaurant, Table
from staff.models import (
    Attendance,
    DailyTableAssignment,
    EmployeeProfile,
    LeaveRequest,
    OrderStaffService,
    PayrollRecord,
    SalaryAdvance,
    Shift,
    StaffTask,
)
from staff.operations import (
    assign_table_to_waiter,
    claim_order_service,
    ensure_order_staff_service,
    handle_order_status_change,
    register_new_order,
)
from staff.performance import get_employee_performance_metrics
from staff.services import (
    ATTENDANCE_TIMEZONE,
    check_in_employee,
    check_out_employee,
    force_check_out_attendance,
    generate_monthly_payroll,
    get_shift_schedule,
)

User = get_user_model()


class StaffWorkflowCompleteTests(TestCase):
    def setUp(self):
        self.restaurant = Restaurant.objects.create(
            name="Spice Garden",
            address="123 Dhaka Ave",
            phone="01700000000",
        )

        self.owner_user = User.objects.create_user(
            username="owner_dev",
            password="testpassword123",
            role="owner",
            is_active=True,
            is_active_staff=True,
            restaurant=self.restaurant,
        )

        self.day_shift = Shift.objects.create(
            restaurant=self.restaurant,
            name="Morning Shift",
            start_time=time(9, 0),
            end_time=time(17, 0),
            grace_minutes=15,
        )

        self.night_shift = Shift.objects.create(
            restaurant=self.restaurant,
            name="Night Shift",
            start_time=time(20, 0),
            end_time=time(4, 0),
            grace_minutes=10,
        )

        # Waiter user
        self.waiter_user = User.objects.create_user(
            username="waiter_rahim",
            password="password123",
            role="waiter",
            is_active=True,
            is_active_staff=True,
            restaurant=self.restaurant,
            first_name="Rahim",
            last_name="Uddin",
        )
        self.waiter_profile = EmployeeProfile.objects.create(
            user=self.waiter_user,
            employee_id="W-101",
            joining_date=date(2026, 9, 1),
            basic_salary=Decimal("30000.00"),
            shift=self.day_shift,
        )

        # Cook user
        self.cook_user = User.objects.create_user(
            username="cook_karim",
            password="password123",
            role="chief",
            is_active=True,
            is_active_staff=True,
            restaurant=self.restaurant,
            first_name="Karim",
            last_name="Mia",
        )
        self.cook_profile = EmployeeProfile.objects.create(
            user=self.cook_user,
            employee_id="C-201",
            joining_date=date(2026, 9, 16),  # Mid-month joining
            basic_salary=Decimal("40000.00"),
            shift=self.day_shift,
        )

        self.table = Table.objects.create(
            restaurant=self.restaurant,
            table_number=1,
            capacity=4,
        )

        self.category = Category.objects.create(
            name="Main Course",
            restaurant=self.restaurant,
        )
        self.item = MenuItem.objects.create(
            name="Biryani",
            price=Decimal("450.00"),
            category=self.category,
        )

        self.client = Client()

    # -------------------------------------------------------------
    # 1. Shift & Schedule Tests
    # -------------------------------------------------------------
    def test_shift_schedule_normal_and_overnight(self):
        # Day shift: 09:00 - 17:00
        ref_dt = timezone.make_aware(
            datetime(2026, 9, 18, 9, 30),
            ATTENDANCE_TIMEZONE,
        )
        work_date, start, end = get_shift_schedule(self.day_shift, ref_dt)
        self.assertEqual(work_date, date(2026, 9, 18))
        self.assertEqual(start.hour, 9)
        self.assertEqual(end.hour, 17)

        # Overnight shift: 20:00 - 04:00 (tested at 01:00 AM)
        overnight_ref = timezone.make_aware(
            datetime(2026, 9, 19, 1, 30),
            ATTENDANCE_TIMEZONE,
        )
        o_date, o_start, o_end = get_shift_schedule(self.night_shift, overnight_ref)
        self.assertEqual(o_date, date(2026, 9, 18))  # Should belong to previous day's shift
        self.assertEqual(o_start.day, 18)
        self.assertEqual(o_end.day, 19)

    def test_early_check_in_window(self):
        # Shift is 09:00 to 17:00
        # Check-in at 08:30 (30m before shift) should be allowed by policy
        now_dt = timezone.make_aware(
            datetime(2026, 9, 18, 8, 30),
            ATTENDANCE_TIMEZONE,
        )
        # Patching now or calling Attendance create directly
        att = Attendance.objects.create(
            employee=self.waiter_profile,
            restaurant=self.restaurant,
            shift=self.day_shift,
            work_date=date(2026, 9, 18),
            scheduled_start=timezone.make_aware(datetime(2026, 9, 18, 9, 0), ATTENDANCE_TIMEZONE),
            scheduled_end=timezone.make_aware(datetime(2026, 9, 18, 17, 0), ATTENDANCE_TIMEZONE),
            grace_minutes=15,
            check_in=now_dt,
        )
        self.assertFalse(att.is_late)
        self.assertEqual(att.late_minutes, 0)
        self.assertEqual(att.arrival_status, "On time")

    # -------------------------------------------------------------
    # 2. Attendance Calculations & Force Checkout
    # -------------------------------------------------------------
    def test_attendance_lateness_and_working_hours(self):
        start = timezone.make_aware(datetime(2026, 9, 18, 9, 0), ATTENDANCE_TIMEZONE)
        end = timezone.make_aware(datetime(2026, 9, 18, 17, 0), ATTENDANCE_TIMEZONE)
        # Arrived at 09:30 (grace is 15 mins -> 15 mins late)
        check_in = timezone.make_aware(datetime(2026, 9, 18, 9, 30), ATTENDANCE_TIMEZONE)
        check_out = timezone.make_aware(datetime(2026, 9, 18, 18, 0), ATTENDANCE_TIMEZONE)  # 8.5h worked

        att = Attendance.objects.create(
            employee=self.waiter_profile,
            restaurant=self.restaurant,
            shift=self.day_shift,
            work_date=date(2026, 9, 18),
            scheduled_start=start,
            scheduled_end=end,
            grace_minutes=15,
            check_in=check_in,
            check_out=check_out,
        )

        self.assertTrue(att.is_late)
        self.assertEqual(att.late_minutes, 15)
        self.assertEqual(att.arrival_status, "Late")
        self.assertEqual(att.worked_minutes, 510)  # 8.5 hours = 510 mins
        self.assertEqual(att.worked_time, "8h 30m")
        self.assertEqual(att.scheduled_minutes, 480)  # 8 hours = 480 mins
        self.assertEqual(att.overtime_minutes, 30)  # 30 mins overtime

    def test_manager_force_checkout(self):
        start = timezone.make_aware(datetime(2026, 9, 18, 9, 0), ATTENDANCE_TIMEZONE)
        end = timezone.make_aware(datetime(2026, 9, 18, 17, 0), ATTENDANCE_TIMEZONE)
        check_in = timezone.make_aware(datetime(2026, 9, 18, 9, 0), ATTENDANCE_TIMEZONE)

        att = Attendance.objects.create(
            employee=self.waiter_profile,
            restaurant=self.restaurant,
            shift=self.day_shift,
            work_date=date(2026, 9, 18),
            scheduled_start=start,
            scheduled_end=end,
            check_in=check_in,
            check_out=None,  # forgotten checkout
        )

        forced_att, changed = force_check_out_attendance(
            self.owner_user,
            att.pk,
            check_out_time=end,
        )
        self.assertTrue(changed)
        self.assertIsNotNone(forced_att.check_out)
        self.assertEqual(forced_att.worked_minutes, 480)

    # -------------------------------------------------------------
    # 3. Leave & Attendance Reconciliation
    # -------------------------------------------------------------
    def test_approved_leave_vs_absence(self):
        leave = LeaveRequest.objects.create(
            employee=self.waiter_profile,
            restaurant=self.restaurant,
            leave_type="annual",
            start_date=date(2026, 9, 10),
            end_date=date(2026, 9, 12),
            reason="Family holiday",
            status="approved",
        )

        self.client.login(username="owner_dev", password="testpassword123")
        response = self.client.get(reverse("staff:attendance_list"), {"date": "2026-09-11"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["on_leave_count"], 1)
        # Rahim on approved annual leave must not be counted as unexcused absence
        self.assertIn(leave, response.context["approved_leaves"])

    # -------------------------------------------------------------
    # 4. Salary Advance & Payroll Deduction Lifecycle
    # -------------------------------------------------------------
    def test_salary_advance_deducted_exactly_once(self):
        # Waiter salary is 30,000 / month
        advance = SalaryAdvance.objects.create(
            employee=self.waiter_profile,
            restaurant=self.restaurant,
            amount=Decimal("5000.00"),
            reason="Medical emergency",
            status="approved",
        )

        # Generate September 2026 payroll
        payroll_month = date(2026, 9, 1)
        result = generate_monthly_payroll(self.restaurant.pk, payroll_month)
        self.assertEqual(len(result["created"]), 2)

        payroll_waiter = PayrollRecord.objects.get(
            employee=self.waiter_profile,
            month=payroll_month,
        )
        self.assertEqual(payroll_waiter.advance_deduction, Decimal("5000.00"))

        advance.refresh_from_db()
        self.assertEqual(advance.payroll_record_id, payroll_waiter.pk)
        self.assertEqual(advance.status, "approved")  # Still approved while draft

        # Mark payroll as paid
        self.client.login(username="owner_dev", password="testpassword123")
        post_resp = self.client.post(reverse("staff:payroll_mark_paid", args=[payroll_waiter.pk]))
        self.assertEqual(post_resp.status_code, 302)

        payroll_waiter.refresh_from_db()
        self.assertEqual(payroll_waiter.status, "paid")

        advance.refresh_from_db()
        self.assertEqual(advance.status, "deducted")  # Finalized!

        # Next month (October 2026) payroll generation
        oct_month = date(2026, 10, 1)
        oct_result = generate_monthly_payroll(self.restaurant.pk, oct_month)
        payroll_oct = PayrollRecord.objects.get(
            employee=self.waiter_profile,
            month=oct_month,
        )
        # Advance must NOT be deducted again!
        self.assertEqual(payroll_oct.advance_deduction, Decimal("0.00"))

    def test_mid_month_joining_and_unpaid_leave_payroll(self):
        # Cook joined on Sept 16 (15 pre-joining days in 30-day month)
        current_today = timezone.localtime(timezone.now(), ATTENDANCE_TIMEZONE).date()
        for day in range(16, current_today.day + 1):
            Attendance.objects.create(
                employee=self.cook_profile,
                restaurant=self.restaurant,
                shift=self.day_shift,
                work_date=date(2026, 9, day),
                scheduled_start=timezone.make_aware(datetime(2026, 9, day, 9, 0), ATTENDANCE_TIMEZONE),
                scheduled_end=timezone.make_aware(datetime(2026, 9, day, 17, 0), ATTENDANCE_TIMEZONE),
                check_in=timezone.make_aware(datetime(2026, 9, day, 9, 0), ATTENDANCE_TIMEZONE),
                check_out=timezone.make_aware(datetime(2026, 9, day, 17, 0), ATTENDANCE_TIMEZONE),
            )

        payroll_month = date(2026, 9, 1)
        generate_monthly_payroll(self.restaurant.pk, payroll_month)

        payroll_cook = PayrollRecord.objects.get(
            employee=self.cook_profile,
            month=payroll_month,
        )
        # Exactly 15 pre-joining days deducted under attendance deduction
        daily_rate = Decimal("40000.00") / Decimal(30)
        expected_deduction = (daily_rate * Decimal(15)).quantize(Decimal("0.01"))
        self.assertAlmostEqual(payroll_cook.attendance_deduction, expected_deduction, places=1)
        self.assertAlmostEqual(payroll_cook.net_salary, Decimal("40000.00") - expected_deduction, places=1)

    def test_draft_payroll_recalculation_and_bonus(self):
        payroll_month = date(2026, 9, 1)
        generate_monthly_payroll(self.restaurant.pk, payroll_month)

        payroll = PayrollRecord.objects.get(
            employee=self.waiter_profile,
            month=payroll_month,
        )
        self.assertEqual(payroll.status, "draft")

        # Adjust bonus via detail view
        self.client.login(username="owner_dev", password="testpassword123")
        adj_resp = self.client.post(
            reverse("staff:payroll_detail", args=[payroll.pk]),
            {"bonus": "2500.00", "note": "Festival bonus"},
        )
        self.assertEqual(adj_resp.status_code, 302)

        payroll.refresh_from_db()
        self.assertEqual(payroll.bonus, Decimal("2500.00"))
        self.assertEqual(payroll.note, "Festival bonus")

        # Recalculate
        recalc_result = generate_monthly_payroll(self.restaurant.pk, payroll_month, recalculate=True)
        self.assertEqual(len(recalc_result["updated"]), 2)
        payroll.refresh_from_db()
        # Bonus preserved!
        self.assertEqual(payroll.bonus, Decimal("2500.00"))

    # -------------------------------------------------------------
    # 5. Table Assignment, Waiter Service & TableSession Handover
    # -------------------------------------------------------------
    def test_table_assignment_and_waiter_order_service(self):
        from staff.operations import local_work_date
        work_date = local_work_date()
        att = Attendance.objects.create(
            employee=self.waiter_profile,
            restaurant=self.restaurant,
            shift=self.day_shift,
            work_date=work_date,
            scheduled_start=timezone.now() - timedelta(hours=1),
            scheduled_end=timezone.now() + timedelta(hours=7),
            check_in=timezone.now() - timedelta(hours=1),
        )

        assignment, created = assign_table_to_waiter(
            actor=self.owner_user,
            attendance_id=att.pk,
            table_id=self.table.pk,
        )
        self.assertTrue(created)
        self.assertEqual(assignment.waiter_id, self.waiter_profile.pk)

        # Create TableSession and Order
        session = TableSession.objects.create(
            table=self.table,
            restaurant=self.restaurant,
            status="OPEN",
        )
        order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            table_session=session,
            status="NEW",
            total_amount=Decimal("450.00"),
        )
        register_new_order(order.pk)

        service = ensure_order_staff_service(order)
        self.assertIsNotNone(service)
        self.assertEqual(service.waiter_id, self.waiter_profile.pk)

        # Order transitions READY -> SERVED -> COMPLETED
        handle_order_status_change(order_id=order.pk, previous_status="NEW", new_status="READY")
        handle_order_status_change(order_id=order.pk, previous_status="READY", new_status="SERVED")
        service.refresh_from_db()
        self.assertIsNotNone(service.served_at)

        handle_order_status_change(order_id=order.pk, previous_status="SERVED", new_status="COMPLETED")
        service.refresh_from_db()
        self.assertIsNotNone(service.completed_at)

    def test_waiter_claim_unassigned_order(self):
        order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            status="NEW",
            total_amount=Decimal("450.00"),
        )

        service = claim_order_service(order.pk, self.waiter_user)
        self.assertEqual(service.waiter_id, self.waiter_profile.pk)
        self.assertEqual(service.order_id, order.pk)

    # -------------------------------------------------------------
    # 6. Performance Metrics Verification
    # -------------------------------------------------------------
    def test_performance_metrics_real_data(self):
        # Create an order served by waiter
        order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            status="COMPLETED",
            total_amount=Decimal("900.00"),
        )
        now = timezone.now()
        svc = OrderStaffService.objects.create(
            order=order,
            waiter=self.waiter_profile,
            completed_at=now,
        )
        OrderStaffService.objects.filter(pk=svc.pk).update(
            assigned_at=now - timedelta(minutes=25),
            ready_at=now - timedelta(minutes=15),
            served_at=now - timedelta(minutes=10),
        )

        start_t = now - timedelta(hours=2)
        att = Attendance.objects.create(
            employee=self.waiter_profile,
            restaurant=self.restaurant,
            shift=self.day_shift,
            work_date=date.today(),
            scheduled_start=start_t,
            scheduled_end=now + timedelta(hours=6),
            grace_minutes=15,
            check_in=start_t,
            check_out=now,
        )
        StaffTask.objects.create(
            restaurant=self.restaurant,
            employee=self.waiter_profile,
            attendance=att,
            title="Clean station",
            work_date=date.today(),
            assigned_by=self.owner_user,
            is_completed=True,
            completed_at=timezone.now(),
        )

        metrics = get_employee_performance_metrics(self.waiter_profile)
        self.assertGreaterEqual(metrics["orders_served"], 1)
        self.assertGreaterEqual(metrics["task_completion_rate"], 100.0)
        self.assertGreater(metrics["avg_ready_to_served_mins"], 0)
        self.assertEqual(metrics["late_count"], 0)

    # -------------------------------------------------------------
    # 7. Authenticated Dev Access to Back-Office
    # -------------------------------------------------------------
    def test_authenticated_dev_access_to_backoffice_pages(self):
        self.client.login(username="owner_dev", password="testpassword123")

        urls_to_test = [
            reverse("staff:employee_list"),
            reverse("staff:employee_detail", args=[self.waiter_user.pk]),
            reverse("staff:shift_list"),
            reverse("staff:attendance_list"),
            reverse("staff:salary_advance_list"),
            reverse("staff:payroll_list"),
            reverse("staff:leave_list"),
            reverse("staff:daily_operations"),
        ]

        for url in urls_to_test:
            resp = self.client.get(url)
            self.assertEqual(
                resp.status_code,
                200,
                f"Expected 200 OK for dev user on {url}, got {resp.status_code}",
            )
