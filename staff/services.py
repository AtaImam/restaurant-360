import calendar
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

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

    return work_date, scheduled_start, scheduled_end


def check_in_employee(actor):
    try:
        with transaction.atomic():
            employee = get_active_employee(actor)

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

            if shift.restaurant_id != employee.user.restaurant_id:
                raise ValidationError(
                    "Your shift does not belong to your restaurant."
                )

            now = timezone.now()
            work_date, start, end = get_shift_schedule(shift, now)

            if not start <= now < end:
                raise ValidationError(
                    "Check-in is available only during your assigned shift."
                )

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

        attendance = Attendance.objects.filter(
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

def generate_monthly_payroll(restaurant_id, payroll_month):
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

    created_records = []
    existing_records = []

    with transaction.atomic():
        for employee in employees:
            basic_salary = employee.basic_salary or Decimal("0.00")

            unpaid_leaves = LeaveRequest.objects.filter(
                employee=employee,
                status="approved",
                leave_type="unpaid",
                start_date__lte=month_end,
                end_date__gte=payroll_month,
            )

            unpaid_days = 0

            for leave_request in unpaid_leaves:
                overlap_start = max(
                    leave_request.start_date,
                    payroll_month,
                )
                overlap_end = min(
                    leave_request.end_date,
                    month_end,
                )

                unpaid_days += (
                    overlap_end - overlap_start
                ).days + 1

            daily_salary = (
                basic_salary / Decimal(days_in_month)
                if days_in_month
                else Decimal("0.00")
            )

            leave_deduction = (
                daily_salary * Decimal(unpaid_days)
            ).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )

            advance_deduction = (
                SalaryAdvance.objects
                .filter(
                    employee=employee,
                    status="approved",
                    created_at__date__lte=month_end,
                )
                .aggregate(total=Sum("amount"))["total"]
                or Decimal("0.00")
            )

            net_salary = max(
                basic_salary
                - leave_deduction
                - advance_deduction,
                Decimal("0.00"),
            ).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )

            payroll_record, created = (
                PayrollRecord.objects.get_or_create(
                    employee=employee,
                    month=payroll_month,
                    defaults={
                        "restaurant_id": restaurant_id,
                        "basic_salary": basic_salary,
                        "bonus": Decimal("0.00"),
                        "attendance_deduction": Decimal("0.00"),
                        "leave_deduction": leave_deduction,
                        "advance_deduction": advance_deduction,
                        "net_salary": net_salary,
                        "status": "draft",
                    },
                )
            )

            if created:
                created_records.append(payroll_record)
            else:
                existing_records.append(payroll_record)

    return {
        "created": created_records,
        "existing": existing_records,
    }
