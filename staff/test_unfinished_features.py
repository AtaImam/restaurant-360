from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from orders.models import Order
from restaurant.models import Branch, Restaurant, Table
from users.models import User
from .forms import EmployeeShiftForm, LeaveRequestForm, ShiftForm
from .models import (Attendance, DailyTableAssignment, EmployeeProfile, LeaveRequest,
                     OrderStaffService, PayrollRecord, SalaryAdvance, Shift, StaffNotification, StaffTask)
from .operations import (assign_staff_task, assign_table_to_waiter, claim_order_service,
                         handle_order_status_change, register_new_order)
from .performance import get_employee_performance_metrics
from .services import (ATTENDANCE_TIMEZONE, check_in_employee, force_check_out_attendance,
                       generate_monthly_payroll, get_shift_schedule)


class UnfinishedStaffRegressionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localtime(timezone.now(), ATTENDANCE_TIMEZONE).date()
        cls.restaurant = Restaurant.objects.create(name='Main restaurant')
        cls.foreign_restaurant = Restaurant.objects.create(name='Other restaurant')
        cls.a = Branch.objects.create(restaurant=cls.restaurant, name='Main', code='A', is_main=True)
        cls.b = Branch.objects.create(restaurant=cls.restaurant, name='City', code='B')
        cls.foreign = Branch.objects.create(restaurant=cls.foreign_restaurant, name='Other', code='O', is_main=True)
        cls.owner = User.objects.create_user(username='owner', role='owner', restaurant=cls.restaurant)
        cls.profiles = {}
        cls.shifts = {}
        cls.tables = {}
        for branch in (cls.a, cls.b, cls.foreign):
            shift = Shift.objects.create(restaurant=branch.restaurant, branch=branch, name='Day', start_time=time(9), end_time=time(17))
            user = User.objects.create_user(username='waiter' + branch.code, first_name='Waiter' + branch.code,
                                             role='waiter', restaurant=branch.restaurant, branch=branch)
            cls.profiles[branch.pk] = EmployeeProfile.objects.create(user=user, employee_id=branch.code,
                joining_date=cls.today - timedelta(days=60), basic_salary=30000, shift=shift)
            cls.shifts[branch.pk] = shift
            cls.tables[branch.pk] = Table.objects.create(restaurant=branch.restaurant, branch=branch, table_number=1, qr_code='tests/table.png')

    def setUp(self):
        self.client.force_login(self.owner)

    def select(self, branch):
        session = self.client.session
        session['active_branch_id'] = branch.pk
        session.save()

    def attendance(self, branch=None, day=None, closed=False, profile=None):
        branch = branch or self.a
        profile = profile or self.profiles[branch.pk]
        day = day or self.today
        start = timezone.make_aware(datetime.combine(day, time(9)), ATTENDANCE_TIMEZONE)
        return Attendance.objects.create(employee=profile, restaurant=branch.restaurant, branch=branch,
            shift=self.shifts[branch.pk], work_date=day, scheduled_start=start, scheduled_end=start + timedelta(hours=8),
            check_in=start, check_out=start + timedelta(hours=8) if closed else None)

    def order(self, branch=None, status='NEW'):
        branch = branch or self.a
        return Order.objects.create(restaurant=branch.restaurant, branch=branch, table=self.tables[branch.pk],
                                    order_type='DINE_IN', status=status, subtotal=100, total_amount=100)

    def payroll(self, branch=None):
        branch = branch or self.a
        return PayrollRecord.objects.create(employee=self.profiles[branch.pk], restaurant=branch.restaurant,
                    branch=branch, month=self.today.replace(day=1), basic_salary=30000, net_salary=30000)

    def leave(self, branch=None, **kwargs):
        branch = branch or self.a
        return LeaveRequest.objects.create(employee=self.profiles[branch.pk], restaurant=branch.restaurant, branch=branch,
            leave_type='annual', start_date=kwargs.pop('start_date', self.today + timedelta(days=1)),
            end_date=kwargs.pop('end_date', self.today + timedelta(days=2)), reason='Leave', **kwargs)

    def test_owner_lists_and_details_follow_active_branch(self):
        for branch in (self.a, self.b, self.foreign):
            self.attendance(branch)
            self.payroll(branch)
            self.leave(branch)
            SalaryAdvance.objects.create(employee=self.profiles[branch.pk], restaurant=branch.restaurant, branch=branch, amount=100)
        for branch in (self.a, self.b):
            self.select(branch)
            profile = self.profiles[branch.pk]
            for name in ('employee_list', 'shift_list', 'attendance_list', 'salary_advance_list', 'payroll_list', 'leave_list'):
                response = self.client.get(reverse('staff:' + name))
                self.assertEqual(response.status_code, 200, name)
                self.assertEqual(response.context['page_obj'].paginator.count, 1, name)
            self.assertEqual(self.client.get(reverse('staff:employee_detail', args=[profile.user_id])).status_code, 200)
            excluded = self.profiles[self.a.pk if branch == self.b else self.b.pk]
            self.assertEqual(self.client.get(reverse('staff:employee_detail', args=[excluded.user_id])).status_code, 404)
            response = self.client.get(reverse('staff:attendance_export_csv'))
            self.assertContains(response, profile.user.first_name)
            self.assertNotContains(response, excluded.user.first_name)

    def test_cross_branch_and_tenant_financial_mutations_are_denied(self):
        for branch in (self.b, self.foreign):
            payroll = self.payroll(branch)
            leave = self.leave(branch)
            advance = SalaryAdvance.objects.create(employee=self.profiles[branch.pk], restaurant=branch.restaurant, branch=branch, amount=100)
            attendance = self.attendance(branch)
            for name, pk, data in [('payroll_detail', payroll.pk, {'bonus': 500}), ('payroll_mark_paid', payroll.pk, {}),
                                  ('leave_review', leave.pk, {'action': 'approve'}), ('salary_advance_review', advance.pk, {'action': 'approve'}),
                                  ('attendance_force_checkout', attendance.pk, {})]:
                self.assertEqual(self.client.post(reverse('staff:' + name, args=[pk]), data).status_code, 404, name)
            payroll.refresh_from_db()
            self.assertEqual(payroll.status, 'draft')
            self.assertEqual(payroll.bonus, 0)

    def test_employee_creation_uses_selected_branch(self):
        self.select(self.b)
        response = self.client.post(reverse('staff:employee_create'), {
            'account-username': 'new_waiter', 'account-first_name': 'New', 'account-role': 'waiter',
            'account-password1': 'StrongStaffPass!9284', 'account-password2': 'StrongStaffPass!9284',
            'profile-employee_id': 'NEW', 'profile-joining_date': str(self.today), 'profile-basic_salary': '10000',
        })
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username='new_waiter')
        self.assertEqual((user.restaurant_id, user.branch_id), (self.restaurant.pk, self.b.pk))

    def test_deactivation_requires_checkout_and_preserves_history(self):
        profile = self.profiles[self.a.pk]
        attendance = self.attendance()
        url = reverse('staff:employee_set_status', args=[profile.user_id])
        self.client.post(url, {'action': 'deactivate'})
        profile.user.refresh_from_db()
        self.assertTrue(profile.user.is_active)
        attendance.check_out = attendance.check_in + timedelta(hours=1)
        attendance.save()
        self.client.post(url, {'action': 'deactivate'})
        profile.user.refresh_from_db()
        self.assertFalse(profile.user.is_active)
        self.assertTrue(Attendance.objects.filter(pk=attendance.pk).exists())
        self.client.post(url, {'action': 'activate'})
        profile.user.refresh_from_db()
        self.assertTrue(profile.user.is_active_staff)

    def test_shift_names_are_unique_per_branch_and_assignment_cannot_cross_branch(self):
        data = {'name': 'Night', 'start_time': '20:00', 'end_time': '04:00', 'grace_minutes': 10}
        self.client.post(reverse('staff:shift_create'), data)
        self.select(self.b)
        self.client.post(reverse('staff:shift_create'), data)
        self.assertEqual(Shift.objects.filter(name='Night').count(), 2)
        duplicate = ShiftForm(data, actor=self.owner, branch=self.b)
        self.assertFalse(duplicate.is_valid())
        profile = self.profiles[self.a.pk]
        self.assertFalse(EmployeeShiftForm({'shift': self.shifts[self.b.pk].pk}, instance=profile).is_valid())
        self.assertTrue(EmployeeShiftForm({'shift': self.shifts[self.a.pk].pk}, instance=profile).is_valid())

    def test_populated_my_attendance_uses_model_working_hour_properties(self):
        record = self.attendance(closed=True)
        response = self.client.get(reverse('staff:my_attendance'), {'employee': record.employee_id})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '8h 0m')
        self.client.force_login(record.employee.user)
        self.assertEqual(self.client.get(reverse('staff:my_attendance')).status_code, 200)

    def test_checkin_rejects_foreign_shift_and_approved_leave(self):
        profile = self.profiles[self.a.pk]
        now = timezone.make_aware(datetime.combine(self.today, time(9)), ATTENDANCE_TIMEZONE)
        profile.shift = self.shifts[self.b.pk]
        profile.save()
        with patch('staff.services.timezone.now', return_value=now), self.assertRaises(ValidationError):
            check_in_employee(profile.user)
        profile.shift = self.shifts[self.a.pk]
        profile.save()
        self.leave(start_date=self.today, end_date=self.today, status='approved')
        with patch('staff.services.timezone.now', return_value=now), self.assertRaises(ValidationError):
            check_in_employee(profile.user)
        self.assertFalse(Attendance.objects.exists())

    def test_midnight_shift_allows_previous_evening_early_checkin(self):
        shift = self.shifts[self.a.pk]
        shift.start_time, shift.end_time = time(0, 30), time(8)
        shift.save()
        now = timezone.make_aware(datetime.combine(self.today, time(23, 45)), ATTENDANCE_TIMEZONE)
        work_date, start, end = get_shift_schedule(shift, now)
        self.assertEqual(work_date, self.today + timedelta(days=1))
        with patch('staff.services.timezone.now', return_value=now):
            record = check_in_employee(self.profiles[self.a.pk].user)
        self.assertEqual(record.work_date, work_date)
        self.assertEqual(record.scheduled_start, start)

    def test_force_checkout_service_checks_tenant_and_branch(self):
        record = self.attendance(self.b)
        with self.assertRaises(PermissionDenied):
            force_check_out_attendance(self.profiles[self.a.pk].user, record.pk)
        record = self.attendance(self.foreign)
        with self.assertRaises(PermissionDenied):
            force_check_out_attendance(self.owner, record.pk)

    def test_leave_validation_review_and_cancellation(self):
        profile = self.profiles[self.a.pk]
        form = LeaveRequestForm({'leave_type': 'annual', 'start_date': str(profile.joining_date - timedelta(days=1)),
             'end_date': str(profile.joining_date), 'reason': 'x'}, employee=profile)
        self.assertFalse(form.is_valid())
        approved = self.leave(status='approved')
        pending = self.leave()
        self.client.post(reverse('staff:leave_review', args=[pending.pk]), {'action': 'approve'})
        pending.refresh_from_db()
        self.assertEqual(pending.status, 'pending')
        self.client.post(reverse('staff:leave_cancel', args=[approved.pk]))
        approved.refresh_from_db()
        self.assertEqual(approved.status, 'cancelled')
        self.client.post(reverse('staff:leave_review', args=[pending.pk]), {'action': 'approve'})
        pending.refresh_from_db()
        self.assertEqual(pending.status, 'approved')

    def test_leave_approval_rejects_attendance_conflict(self):
        self.attendance()
        leave = self.leave(start_date=self.today, end_date=self.today)
        self.client.post(reverse('staff:leave_review', args=[leave.pk]), {'action': 'approve'})
        leave.refresh_from_db()
        self.assertEqual(leave.status, 'pending')

    def test_salary_advance_review_first_and_manual_form_error_visibility(self):
        url = reverse('staff:salary_advance_list')
        response = self.client.get(url)
        html = response.content.decode()
        self.assertContains(response, 'Create Manual Advance')
        self.assertNotContains(response, 'Create Advance Request')
        self.assertIn('<details class="advance-card manual-advance">', html)
        self.assertLess(html.index('Requests &amp; History'), html.index('<details'))
        invalid = self.client.post(url, {'amount': '-1'})
        self.assertContains(invalid, '<details class="advance-card manual-advance" open>')
        advance = SalaryAdvance.objects.create(
            employee=self.profiles[self.a.pk], restaurant=self.restaurant,
            branch=self.a, amount=100, reason='Travel',
        )
        SalaryAdvance.objects.create(
            employee=self.profiles[self.a.pk], restaurant=self.restaurant,
            branch=self.a, amount=50, status='approved', reason='Older review',
        )
        pending = self.client.get(url)
        self.assertEqual(pending.context['page_obj'][0].pk, advance.pk)
        self.assertContains(pending, 'value="approve"')
        self.assertContains(pending, 'value="reject"')
        self.client.post(reverse('staff:salary_advance_review', args=[advance.pk]), {'action': 'reject'})
        advance.refresh_from_db()
        self.assertEqual(advance.status, 'rejected')
        self.assertIsNone(advance.payroll_record_id)

    def test_salary_advance_choices_and_duplicate_pending_validation(self):
        url = reverse('staff:salary_advance_list')
        self.client.post(url, {'employee': self.profiles[self.b.pk].pk, 'amount': 100, 'reason': 'x'})
        self.assertFalse(SalaryAdvance.objects.exists())
        data = {'employee': self.profiles[self.a.pk].pk, 'amount': 100, 'reason': 'x'}
        self.client.post(url, data)
        self.client.post(url, data)
        self.assertEqual(SalaryAdvance.objects.count(), 1)
        advance = SalaryAdvance.objects.get()
        review = reverse('staff:salary_advance_review', args=[advance.pk])
        self.client.post(review, {'action': 'approve'})
        self.client.post(review, {'action': 'reject'})
        advance.refresh_from_db()
        self.assertEqual(advance.status, 'approved')

    def test_payroll_generation_is_branch_scoped_and_paid_records_stay_immutable(self):
        month = self.today.replace(day=1)
        self.client.post(reverse('staff:payroll_list'), {'action': 'generate', 'month': month.strftime('%Y-%m')})
        self.assertEqual(list(PayrollRecord.objects.values_list('branch_id', flat=True)), [self.a.pk])
        payroll = PayrollRecord.objects.get()
        self.client.post(reverse('staff:payroll_mark_paid', args=[payroll.pk]))
        payroll.refresh_from_db()
        paid_at = payroll.paid_at
        net = payroll.net_salary
        self.client.post(reverse('staff:payroll_detail', args=[payroll.pk]), {'bonus': 100})
        generate_monthly_payroll(self.restaurant.pk, month, recalculate=True, branch=self.a)
        self.client.post(reverse('staff:payroll_mark_paid', args=[payroll.pk]))
        payroll.refresh_from_db()
        self.assertEqual((payroll.net_salary, payroll.paid_at, payroll.bonus), (net, paid_at, 0))

    def test_excess_advance_is_carried_forward_without_being_written_off(self):
        profile = self.profiles[self.a.pk]
        payroll = self.payroll()
        payroll.attendance_deduction = 29950
        payroll.net_salary = 50
        payroll.save()
        advance = SalaryAdvance.objects.create(employee=profile, restaurant=self.restaurant, branch=self.a, amount=100, status='approved', payroll_record=payroll)
        payroll.advance_deduction = 100
        payroll.net_salary = 0
        payroll.save()
        self.client.post(reverse('staff:payroll_mark_paid', args=[payroll.pk]))
        payroll.refresh_from_db()
        advance.refresh_from_db()
        self.assertEqual(payroll.status, 'draft')
        self.assertEqual(advance.status, 'approved')
        # An older fully absent month has no earnings to recover this advance from.
        month = (self.today.replace(day=1) - timedelta(days=1)).replace(day=1)
        SalaryAdvance.objects.filter(pk=advance.pk).update(payroll_record=None, created_at=timezone.make_aware(datetime.combine(month, time(12)), ATTENDANCE_TIMEZONE))
        result = generate_monthly_payroll(self.restaurant.pk, month, branch=self.a)
        self.assertEqual(result['created'][0].advance_deduction, 0)
        advance.refresh_from_db()
        self.assertIsNone(advance.payroll_record_id)
        self.assertEqual(advance.status, 'approved')

    def test_bonus_validation_rejects_nonfinite_excess_and_fractional_cents(self):
        payroll = self.payroll()
        for bonus in ('NaN', 'Infinity', '-1', '99999999999999', '1.001'):
            response = self.client.post(reverse('staff:payroll_detail', args=[payroll.pk]), {'bonus': bonus})
            self.assertEqual(response.status_code, 302)
            payroll.refresh_from_db()
            self.assertEqual(payroll.bonus, 0)

    def test_table_and_task_assignment_reject_cross_branch_records(self):
        record = self.attendance()
        with self.assertRaises(ValidationError):
            assign_table_to_waiter(actor=self.owner, attendance_id=record.pk, table_id=self.tables[self.b.pk].pk)
        foreign_order = self.order(self.b)
        with self.assertRaises(ValidationError):
            assign_staff_task(actor=self.owner, attendance_id=record.pk, title='Serve', related_order_id=foreign_order.pk)
        task = assign_staff_task(actor=self.owner, attendance_id=record.pk, title='Prepare tables')
        self.assertEqual(task.branch_id, self.a.pk)
        self.assertEqual(StaffNotification.objects.get(event_key__startswith='staff-task:').branch_id, self.a.pk)

    def test_overnight_waiter_remains_assignable_after_midnight(self):
        record = self.attendance(day=self.today - timedelta(days=1))
        assignment, _ = assign_table_to_waiter(actor=self.owner, attendance_id=record.pk, table_id=self.tables[self.a.pk].pk)
        response = self.client.get(reverse('staff:daily_operations'))
        self.assertIn(record, response.context['open_attendance'])
        self.assertIn(assignment, response.context['table_assignments'])
        self.assertIn(record, response.context['table_form'].fields['attendance'].queryset)

    def test_owner_can_assign_unclaimed_orders_and_served_history_cannot_be_overwritten(self):
        profile = self.profiles[self.a.pk]
        order = self.order()
        self.client.post(reverse('staff:waiter_claim_order', args=[order.pk]), {'waiter_id': profile.user_id})
        service = OrderStaffService.objects.get(order=order)
        self.assertEqual(service.waiter, profile)
        Order.objects.filter(pk=order.pk).update(status='SERVED')
        with self.assertRaises(ValidationError):
            claim_order_service(order.pk, profile.user)
        response = self.client.get(reverse('staff:employee_detail', args=[profile.user_id]))
        self.assertIn(service, response.context['service_history'])
        self.assertContains(response, 'Waiter Service History')
        with self.assertRaises(PermissionDenied):
            claim_order_service(self.order(self.b).pk, profile.user)

    def test_metrics_use_ready_timestamp_and_exclude_unknown_history(self):
        profile = self.profiles[self.a.pk]
        order = self.order()
        service = claim_order_service(order.pk, profile.user)
        now = timezone.now()
        ready = now - timedelta(minutes=5)
        Order.objects.filter(pk=order.pk).update(status='READY', status_changed_at=ready)
        handle_order_status_change(order_id=order.pk, previous_status='PREPARING', new_status='READY')
        OrderStaffService.objects.filter(pk=service.pk).update(served_at=now)
        Order.objects.filter(pk=order.pk).update(status='SERVED')
        legacy = self.order(status='SERVED')
        OrderStaffService.objects.create(order=legacy, waiter=profile, served_at=now)
        metrics = get_employee_performance_metrics(profile, branch=self.a)
        self.assertEqual(metrics['avg_ready_to_served_mins'], 5)
        self.assertEqual(metrics['service_timing_samples'], 1)
        self.assertEqual(metrics['orders_served'], 2)

    def test_approved_leave_is_not_counted_as_missed_attendance_in_metrics(self):
        self.attendance(closed=True)
        self.leave(start_date=self.today - timedelta(days=1), end_date=self.today - timedelta(days=1), status='approved')
        metrics = get_employee_performance_metrics(self.profiles[self.a.pk], self.today - timedelta(days=1), self.today, branch=self.a)
        self.assertEqual(metrics['attendance_rate'], 100)
        self.assertEqual(metrics['expected_days'], 1)

    def test_notifications_are_branch_scoped_deduplicated_and_read_once(self):
        for branch in (self.a, self.b):
            user = User.objects.create_user(username='cook' + branch.code, role='chief', restaurant=self.restaurant, branch=branch)
            profile = EmployeeProfile.objects.create(user=user, employee_id='cook' + branch.code, joining_date=self.today, shift=self.shifts[branch.pk])
            self.attendance(branch, profile=profile)
        order = self.order()
        register_new_order(order.pk)
        register_new_order(order.pk)
        self.assertEqual(StaffNotification.objects.filter(order=order).count(), 2)
        self.assertFalse(StaffNotification.objects.filter(recipient__username='cookB').exists())
        notification = StaffNotification.objects.get(order=order, recipient=self.owner)
        url = reverse('staff:notification_mark_read', args=[notification.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.post(url)
        notification.refresh_from_db()
        read_at = notification.read_at
        self.client.post(url)
        notification.refresh_from_db()
        self.assertEqual(notification.read_at, read_at)
        self.assertTrue(notification.is_read)
        self.select(self.b)
        self.assertEqual(self.client.post(url).status_code, 404)

    def test_task_completion_is_idempotent_and_branch_scoped(self):
        record = self.attendance()
        task = assign_staff_task(actor=self.owner, attendance_id=record.pk, title='Clean')
        url = reverse('staff:daily_task_complete', args=[task.pk])
        self.select(self.b)
        self.assertEqual(self.client.post(url).status_code, 404)
        self.select(self.a)
        self.client.post(url)
        task.refresh_from_db()
        timestamp = task.completed_at
        self.client.post(url)
        task.refresh_from_db()
        self.assertEqual(task.completed_at, timestamp)
        self.assertTrue(task.is_completed)

    def test_invalid_calendar_filter_does_not_crash(self):
        self.assertEqual(self.client.get(reverse('staff:attendance_list'), {'date': '2026-99-99'}).status_code, 200)
        self.assertEqual(self.client.get(reverse('staff:attendance_export_csv'), {'from_date': '2026-99-99'}).status_code, 200)

    def test_owner_handover_preserves_served_session_order_history(self):
        original = self.profiles[self.a.pk]
        replacement_user = User.objects.create_user(username="replacement", role="waiter", restaurant=self.restaurant, branch=self.a)
        replacement = EmployeeProfile.objects.create(user=replacement_user, employee_id="replacement", joining_date=self.today, shift=self.shifts[self.a.pk])
        served_order = self.order(status="SERVED")
        history = OrderStaffService.objects.create(order=served_order, waiter=original, served_at=timezone.now())
        active_order = self.order(status="READY")
        active_service = OrderStaffService.objects.create(order=active_order, waiter=original)
        self.assertEqual(served_order.table_session_id, active_order.table_session_id)
        self.assertContains(self.client.get(reverse("staff:daily_operations")), "Hand Over")
        response = self.client.post(reverse("staff:waiter_claim_order", args=[active_order.pk]), {"waiter_id": replacement_user.pk})
        self.assertEqual(response.status_code, 302)
        active_service.refresh_from_db()
        history.refresh_from_db()
        self.assertEqual(active_service.waiter_id, replacement.pk)
        self.assertEqual(history.waiter_id, original.pk)
