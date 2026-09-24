import calendar
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from .access import ensure_staff_record_access
from .models import (
    Attendance,
    EmployeeProfile,
    LeaveRequest,
    PayrollRecord,
    SalaryAdvance,
)


User = get_user_model()
ATTENDANCE_TIMEZONE = ZoneInfo("Asia/Dhaka")


def get_active_employee(actor):
    if not actor.is_authenticated:
        raise PermissionDenied("Please log in.")

    user = User.objects.get(pk=actor.pk)

    if not user.is_active or not user.is_active_staff:
        raise PermissionDenied("Your staff account is inactive.")

    employee = (
        EmployeeProfile.objects
        .select_related("user", "shift")
        .filter(user=user)
        .first()
    )

    if employee is None:
        raise ValidationError(
            "Your employee profile has not been completed."
        )

    if not user.restaurant_id:
        raise ValidationError(
            "Your account is not assigned to a restaurant."
        )

    return employee


def get_shift_schedule(shift, now):
    local_now = timezone.localtime(now, ATTENDANCE_TIMEZONE)
    work_date = local_now.date()
    overnight = shift.end_time < shift.start_time

    if overnight and local_now.time() < shift.end_time:
        work_date -= timedelta(days=1)

    end_date = work_date + timedelta(days=1) if overnight else work_date

    scheduled_start = timezone.make_aware(
        datetime.combine(work_date, shift.start_time),
        ATTENDANCE_TIMEZONE,
    )
    scheduled_end = timezone.make_aware(
        datetime.combine(end_date, shift.end_time),
        ATTENDANCE_TIMEZONE,
    )

    # A shift beginning shortly after midnight can be checked into the prior evening.
    if scheduled_start + timedelta(days=1, minutes=-60) <= now < scheduled_start + timedelta(days=1):
        work_date += timedelta(days=1)
        scheduled_start += timedelta(days=1)
        scheduled_end += timedelta(days=1)
    return work_date, scheduled_start, scheduled_end


def check_in_employee(actor):
    try:
        with transaction.atomic():
            employee = get_active_employee(actor)
            EmployeeProfile.objects.select_for_update().get(pk=employee.pk)

            if Attendance.objects.filter(
                employee=employee,
                check_out__isnull=True,
            ).exists():
                raise ValidationError(
                    "You are already checked in. Check out first."
                )

            shift = employee.shift

            if shift is None or not shift.is_active:
                raise ValidationError(
                    "Ask your manager to assign an active shift."
                )

            if shift.restaurant_id != employee.user.restaurant_id or shift.branch_id != employee.user.branch_id:
                raise ValidationError(
                    "Your shift does not belong to your restaurant."
                )

            now = timezone.now()
            work_date, start, end = get_shift_schedule(shift, now)

            earliest_check_in = start - timedelta(minutes=60)
            if not earliest_check_in <= now < end:
                raise ValidationError(
                    "Check-in is available starting 60 minutes before your shift until shift end."
                )

            if LeaveRequest.objects.filter(employee=employee, status="approved", start_date__lte=work_date, end_date__gte=work_date).exists():
                raise ValidationError("You cannot check in while on approved leave.")

            if work_date < employee.joining_date:
                raise ValidationError(
                    "You cannot check in before your joining date."
                )

            if Attendance.objects.filter(
                employee=employee,
                work_date=work_date,
            ).exists():
                raise ValidationError(
                    "Attendance already exists for this duty date."
                )

            return Attendance.objects.create(
                employee=employee,
                restaurant_id=employee.user.restaurant_id,
                shift=shift,
                work_date=work_date,
                scheduled_start=start,
                scheduled_end=end,
                grace_minutes=shift.grace_minutes,
                check_in=now,
            )

    except IntegrityError as exc:
        raise ValidationError(
            "Attendance could not be created. Refresh the page "
            "and check whether you are already checked in."
        ) from exc


def check_out_employee(actor):
    with transaction.atomic():
        employee = get_active_employee(actor)

        attendance = Attendance.objects.select_for_update().filter(
            employee=employee,
            check_out__isnull=True,
        ).first()

        if attendance is None:
            raise ValidationError("You have no open check-in.")

        now = timezone.now()

        if now < attendance.check_in:
            raise ValidationError(
                "Server time is earlier than check-in. Contact your manager."
            )

        attendance.check_out = now
        attendance.save(update_fields=["check_out", "updated_at"])

        return attendance


def force_check_out_attendance(actor, attendance_id, check_out_time=None):
    """Manager or dev user force checkout for an unclosed attendance."""
    with transaction.atomic():
        attendance = (
            Attendance.objects
            .select_for_update()
            .select_related("employee__user", "shift")
            .get(pk=attendance_id)
        )

        ensure_staff_record_access(actor, attendance.restaurant_id, attendance.branch_id)
        if attendance.check_out is not None:
            return attendance, False

        now = timezone.now()
        checkout = check_out_time or attendance.scheduled_end
        if checkout > now:
            checkout = now
        if checkout < attendance.check_in:
            checkout = attendance.check_in

        attendance.check_out = checkout
        attendance.save(update_fields=["check_out", "updated_at"])
        return attendance, True


def generate_monthly_payroll(restaurant_id, payroll_month, recalculate=False, branch=None):
    payroll_month = payroll_month.replace(day=1)

    days_in_month = calendar.monthrange(
        payroll_month.year,
        payroll_month.month,
    )[1]

    month_end = payroll_month.replace(day=days_in_month)

    employees = (
        EmployeeProfile.objects
        .filter(
            user__restaurant_id=restaurant_id,
            user__is_active=True,
            user__is_active_staff=True,
        )
        .select_related("user")
        .order_by("employee_id")
    )

    if branch is not None:
        if branch.restaurant_id != restaurant_id:
            raise ValidationError("The payroll branch belongs to another restaurant.")
        employees = employees.filter(user__branch=branch)
    created_records = []
    updated_records = []
    existing_records = []

    local_now = timezone.localtime(timezone.now(), ATTENDANCE_TIMEZONE)
    local_today = local_now.date()
    eval_end_date = min(month_end, local_today)

    with transaction.atomic():
        for employee in employees.select_for_update():
            if employee.joining_date > month_end:
                continue

            basic_salary = employee.basic_salary or Decimal("0.00")
            daily_salary = (
                basic_salary / Decimal(days_in_month)
                if days_in_month
                else Decimal("0.00")
            )

            # Mid-month joining proration
            effective_start = max(payroll_month, employee.joining_date)
            pre_joining_days = max(0, (effective_start - payroll_month).days)

            # Approved leaves in month
            approved_leaves = list(
                LeaveRequest.objects.filter(
                    employee=employee,
                    restaurant_id=restaurant_id,
                    branch_id=employee.user.branch_id,
                    status="approved",
                    start_date__lte=month_end,
                    end_date__gte=payroll_month,
                )
            )

            unpaid_leave_days = 0
            paid_leave_days = 0
            leave_covered_dates = set()

            for leave_request in approved_leaves:
                overlap_start = max(leave_request.start_date, effective_start)
                overlap_end = min(leave_request.end_date, month_end)
                if overlap_start <= overlap_end:
                    days_count = (overlap_end - overlap_start).days + 1
                    if leave_request.leave_type == "unpaid":
                        unpaid_leave_days += days_count
                    else:
                        paid_leave_days += days_count

                    cur = max(leave_request.start_date, effective_start)
                    lim = min(leave_request.end_date, eval_end_date)
                    while cur <= lim:
                        leave_covered_dates.add(cur)
                        cur += timedelta(days=1)

            leave_deduction = (
                daily_salary * Decimal(unpaid_leave_days)
            ).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )

            # Attendance records
            attended_dates = set(
                Attendance.objects.filter(
                    employee=employee,
                    restaurant_id=restaurant_id,
                    branch_id=employee.user.branch_id,
                    work_date__gte=effective_start,
                    work_date__lte=eval_end_date,
                ).values_list("work_date", flat=True)
            )

            # Unexcused absences = days where employee neither attended nor had approved leave
            unexcused_absent_days = 0
            if eval_end_date >= effective_start:
                cur_day = effective_start
                while cur_day <= eval_end_date:
                    if cur_day not in attended_dates and cur_day not in leave_covered_dates:
                        unexcused_absent_days += 1
                    cur_day += timedelta(days=1)

            total_absent_days = unexcused_absent_days + pre_joining_days
            attendance_deduction = (
                daily_salary * Decimal(total_absent_days)
            ).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )

            existing_record = PayrollRecord.objects.select_for_update().filter(
                employee=employee,
                month=payroll_month,
            ).first()

            if existing_record:
                if existing_record.branch_id != employee.user.branch_id:
                    existing_records.append(existing_record)
                    continue
                if existing_record.status == "paid":
                    existing_records.append(existing_record)
                    continue
                elif not recalculate:
                    existing_records.append(existing_record)
                    continue
                payroll_record = existing_record
            else:
                payroll_record = PayrollRecord(
                    employee=employee,
                    month=payroll_month,
                    restaurant_id=restaurant_id,
                    branch_id=employee.user.branch_id,
                    status="draft",
                    basic_salary=basic_salary,
                    bonus=Decimal("0.00"),
                )

            # Link & deduct unlinked approved advances created on or before month_end,
            # or advances already linked to this payroll record
            advances_to_deduct = list(
                SalaryAdvance.objects.select_for_update().filter(
                    employee=employee,
                    restaurant_id=restaurant_id,
                    branch_id=employee.user.branch_id,
                    status="approved",
                ).filter(
                    (models.Q(payroll_record=payroll_record) if payroll_record.pk else models.Q(pk__in=[]))
                    | models.Q(payroll_record__isnull=True, created_at__date__lte=month_end)
                )
            )

            # Only recover whole advances that fit the earnings available. Others
            # remain approved and unlinked for a later payroll instead of being lost.
            bonus = payroll_record.bonus or Decimal("0.00")
            available = max(basic_salary + bonus - attendance_deduction - leave_deduction, Decimal("0.00"))
            recoverable = []
            for advance in sorted(advances_to_deduct, key=lambda row: (row.created_at, row.pk)):
                if advance.amount <= available:
                    recoverable.append(advance)
                    available -= advance.amount
                elif advance.payroll_record_id == payroll_record.pk and payroll_record.pk:
                    advance.payroll_record = None
                    advance.save(update_fields=["payroll_record"])
            advances_to_deduct = recoverable

            advance_deduction = sum(
                (adv.amount for adv in advances_to_deduct),
                Decimal("0.00"),
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            bonus = payroll_record.bonus or Decimal("0.00")

            net_salary = max(
                basic_salary
                + bonus
                - attendance_deduction
                - leave_deduction
                - advance_deduction,
                Decimal("0.00"),
            ).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )

            payroll_record.basic_salary = basic_salary
            payroll_record.attendance_deduction = attendance_deduction
            payroll_record.leave_deduction = leave_deduction
            payroll_record.advance_deduction = advance_deduction
            payroll_record.net_salary = net_salary
            payroll_record.restaurant_id = restaurant_id
            payroll_record.save()

            for adv in advances_to_deduct:
                if adv.payroll_record_id != payroll_record.pk:
                    adv.payroll_record = payroll_record
                    adv.save(update_fields=["payroll_record"])

            if existing_record:
                updated_records.append(payroll_record)
            else:
                created_records.append(payroll_record)

    return {
        "created": created_records,
        "updated": updated_records,
        "existing": existing_records,
    }
