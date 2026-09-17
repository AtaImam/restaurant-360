import csv
from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.urls import reverse
from django.shortcuts import get_object_or_404, redirect, render
from django.http import HttpResponse
from django.urls import reverse
from django.contrib import messages
from django.db import IntegrityError, transaction
from django.views.decorators.http import require_http_methods
from .models import (
    Attendance,
    EmployeeProfile,
    LeaveRequest,
    PayrollRecord,
    SalaryAdvance,
    Shift,
)
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.contrib.auth.decorators import login_required

from .forms import (
    EmployeeAccountForm,
    EmployeeAccountEditForm,
    EmployeeProfileForm,
    StaffLeaveRequestForm,
    EmployeeShiftForm,
    LeaveRequestForm,
    SalaryAdvanceForm,
    ShiftForm,
)

from restaurant.models import Restaurant
from .services import (
    ATTENDANCE_TIMEZONE,
    check_in_employee,
    check_out_employee,
    generate_monthly_payroll,
    get_active_employee,
)

from django.contrib.auth.decorators import login_required


User = get_user_model()

EMPLOYEE_ROLES = [
    "manager",
    "waiter",
    "chief",
    "kitchen_manager",
    "bar_manager",
]


@login_required
def employee_list(request):
    if not (
        request.user.is_superuser
        or request.user.role in ["admin", "owner", "manager"]
    ):
        raise PermissionDenied(
            "You do not have permission to manage employees."
        )
    if not request.user.is_active or not request.user.is_active_staff:
        raise PermissionDenied("Your staff account is inactive.")

    employees = (
        User.objects
        .filter(role__in=EMPLOYEE_ROLES, is_superuser=False)
        .select_related("restaurant", "employee_profile")
        .order_by("first_name", "last_name", "username")
    )

    if not (request.user.is_superuser or request.user.role == "admin"):
        if not request.user.restaurant_id:
            raise PermissionDenied(
                "Your account is not assigned to a restaurant."
            )

        employees = employees.filter(
            restaurant_id=request.user.restaurant_id
        )

    total_staff = employees.count()

    active_staff = employees.filter(
        is_active=True,
        is_active_staff=True,
    ).count()

    monthly_payroll = (
        employees.filter(
            is_active=True,
            is_active_staff=True,
        ).aggregate(
            total=Sum("employee_profile__basic_salary")
        )["total"]
        or 0
    )

    search = request.GET.get("search", "").strip()

    if search:
        employees = employees.filter(
            Q(first_name__icontains=search)
            | Q(last_name__icontains=search)
            | Q(username__icontains=search)
            | Q(phone__icontains=search)
            | Q(employee_profile__employee_id__icontains=search)
        )

    paginator = Paginator(employees, 20)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "staff/employee_list.html",
        {
            "page_obj": page_obj,
            "search": search,
            "total_staff": total_staff,
            "active_staff": active_staff,
            "monthly_payroll": monthly_payroll,
            "result_count": paginator.count,
        },
    )

@login_required
@require_http_methods(["GET", "POST"])
def employee_create(request):
    data = request.POST if request.method == "POST" else None

    account_form = EmployeeAccountForm(
        data=data,
        actor=request.user,
        prefix="account",
    )
    profile_form = EmployeeProfileForm(
        data=data,
        prefix="profile",
    )

    if request.method == "POST":
        account_valid = account_form.is_valid()
        profile_valid = profile_form.is_valid()

        if account_valid and profile_valid:
            try:
                with transaction.atomic():
                    employee = account_form.save(commit=False)
                    employee.is_staff = False
                    employee.is_superuser = False
                    employee.is_active = True
                    employee.is_active_staff = True
                    employee.save()

                    profile = profile_form.save(commit=False)
                    profile.user = employee
                    profile.save()

            except IntegrityError:
                account_form.add_error(
                    None,
                    "Could not save the employee. The username or "
                    "employee ID may have just been used. "
                    "Check the details and try again.",
                )
            else:
                messages.success(
                    request,
                    "Employee added successfully.",
                )
                return redirect("staff:employee_list")

    return render(
        request,
        "staff/employee_form.html",
        {
            "account_form": account_form,
            "profile_form": profile_form,
        },
    )

@login_required
@require_http_methods(["GET"])
def employee_detail(request, employee_id):
    actor = request.user

    if not actor.is_active or not actor.is_active_staff:
        raise PermissionDenied("Your staff account is inactive.")

    if not (
        actor.is_superuser
        or actor.role in ["admin", "owner", "manager"]
    ):
        raise PermissionDenied(
            "You do not have permission to view employee details."
        )

    employees = (
        User.objects
        .filter(role__in=EMPLOYEE_ROLES, is_superuser=False)
        .select_related("restaurant", "employee_profile")
    )

    if not (actor.is_superuser or actor.role == "admin"):
        if not actor.restaurant_id:
            raise PermissionDenied(
                "Your account is not assigned to a restaurant."
            )

        employees = employees.filter(
            restaurant_id=actor.restaurant_id
        )

    employee = get_object_or_404(employees, pk=employee_id)
    profile = getattr(employee, "employee_profile", None)

    return render(
        request,
        "staff/employee_detail.html",
        {
            "employee": employee,
            "profile": profile,
        },
    )

@login_required
@require_http_methods(["GET", "POST"])
def employee_edit(request, employee_id):
    actor = request.user

    if not actor.is_active or not actor.is_active_staff:
        raise PermissionDenied("Your staff account is inactive.")

    if not (
        actor.is_superuser
        or actor.role in ["admin", "owner", "manager"]
    ):
        raise PermissionDenied("You cannot edit employees.")

    employees = User.objects.filter(
        role__in=EMPLOYEE_ROLES,
        is_superuser=False,
    ).select_related("employee_profile")

    if not (actor.is_superuser or actor.role == "admin"):
        if not actor.restaurant_id:
            raise PermissionDenied(
                "Your account is not assigned to a restaurant."
            )

        employees = employees.filter(
            restaurant_id=actor.restaurant_id
        )

        if actor.role == "manager":
            employees = employees.exclude(role="manager")

    employee = get_object_or_404(employees, pk=employee_id)
    profile = getattr(employee, "employee_profile", None)
    data = request.POST if request.method == "POST" else None

    account_form = EmployeeAccountEditForm(
        data=data,
        instance=employee,
        actor=actor,
        prefix="account",
    )
    profile_form = EmployeeProfileForm(
        data=data,
        instance=profile,
        prefix="profile",
    )

    if request.method == "POST":
        account_valid = account_form.is_valid()
        profile_valid = profile_form.is_valid()

        if account_valid and profile_valid:
            try:
                with transaction.atomic():
                    account_form.save()

                    updated_profile = profile_form.save(commit=False)
                    updated_profile.user = employee
                    updated_profile.save()

            except IntegrityError:
                profile_form.add_error(
                    None,
                    "Could not save changes. The employee ID may "
                    "already be in use. Check the details and try again.",
                )
            else:
                messages.success(
                    request,
                    "Employee updated successfully.",
                )
                return redirect(
                    "staff:employee_detail",
                    employee_id=employee.pk,
                )

    return render(
        request,
        "staff/employee_form.html",
        {
            "account_form": account_form,
            "profile_form": profile_form,
            "is_edit": True,
            "employee": employee,
        },
    )
@login_required
@require_http_methods(["POST"])
def employee_set_status(request, employee_id):
    actor = request.user

    if not actor.is_active or not actor.is_active_staff:
        raise PermissionDenied("Your staff account is inactive.")

    if not (
        actor.is_superuser
        or actor.role in ["admin", "owner", "manager"]
    ):
        raise PermissionDenied("You cannot change employee status.")

    action = request.POST.get("action")

    if action not in ["activate", "deactivate"]:
        raise PermissionDenied("Invalid status action.")

    with transaction.atomic():
        employees = User.objects.filter(
            role__in=EMPLOYEE_ROLES,
            is_superuser=False,
        )

        if not (actor.is_superuser or actor.role == "admin"):
            if not actor.restaurant_id:
                raise PermissionDenied(
                    "Your account is not assigned to a restaurant."
                )

            employees = employees.filter(
                restaurant_id=actor.restaurant_id
            )

            if actor.role == "manager":
                employees = employees.exclude(role="manager")

        employee = get_object_or_404(
            employees,
            pk=employee_id,
        )

        if employee.pk == actor.pk:
            raise PermissionDenied(
                "You cannot change your own account status here."
            )

        active = action == "activate"
        employee.is_active = active
        employee.is_active_staff = active
        employee.save(
            update_fields=["is_active", "is_active_staff"]
        )

    messages.success(
        request,
        "Employee activated successfully."
        if active
        else "Employee deactivated successfully.",
    )

    return redirect(
        "staff:employee_detail",
        employee_id=employee.pk,
    )
def require_shift_manager(actor):
    if (
        not actor.is_authenticated
        or not actor.is_active
        or not actor.is_active_staff
    ):
        raise PermissionDenied("An active staff account is required.")

    if not (
        actor.is_superuser
        or actor.role in ["admin", "owner", "manager"]
    ):
        raise PermissionDenied("You cannot manage shifts.")

    if not (actor.is_superuser or actor.role == "admin"):
        if not actor.restaurant_id:
            raise PermissionDenied(
                "Your account is not assigned to a restaurant."
            )


@login_required
@require_http_methods(["GET"])
def shift_list(request):
    require_shift_manager(request.user)

    shifts = (
        Shift.objects
        .select_related("restaurant")
        .annotate(
            assigned_count=Count(
                "employees",
                filter=Q(
                    employees__user__is_active=True,
                    employees__user__is_active_staff=True,
                ),
            )
        )
        .order_by(
            "restaurant__name",
            "start_time",
            "pk",
        )
    )

    if not (
        request.user.is_superuser
        or request.user.role == "admin"
    ):
        shifts = shifts.filter(
            restaurant_id=request.user.restaurant_id
        )

    total_shifts = shifts.count()
    active_shifts = shifts.filter(is_active=True).count()
    inactive_shifts = shifts.filter(is_active=False).count()

    paginator = Paginator(shifts, 20)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "staff/shift_list.html",
        {
            "page_obj": page_obj,
            "total_shifts": total_shifts,
            "active_shifts": active_shifts,
            "inactive_shifts": inactive_shifts,
        },
    )



@login_required
@require_http_methods(["GET", "POST"])
def shift_create(request):
    require_shift_manager(request.user)

    data = request.POST if request.method == "POST" else None

    form = ShiftForm(
        data=data,
        actor=request.user,
    )

    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                shift = form.save(commit=False)
                shift.is_active = True
                shift.save()

        except IntegrityError:
            form.add_error(
                None,
                "Could not save the shift. Check whether this "
                "restaurant already has a shift with the same name.",
            )
        else:
            messages.success(
                request,
                "Shift created successfully.",
            )
            return redirect("staff:shift_list")

    return render(
        request,
        "staff/shift_form.html",
        {"form": form},
    )


@login_required
@require_http_methods(["GET", "POST"])
def shift_edit(request, shift_id):
    require_shift_manager(request.user)

    shifts = Shift.objects.select_related("restaurant")

    if not (
        request.user.is_superuser
        or request.user.role == "admin"
    ):
        shifts = shifts.filter(
            restaurant_id=request.user.restaurant_id
        )

    shift = get_object_or_404(shifts, pk=shift_id)

    form = ShiftForm(
        request.POST if request.method == "POST" else None,
        instance=shift,
        actor=request.user,
    )

    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                shift = form.save()
        except IntegrityError:
            form.add_error(
                None,
                "Could not update the shift. Check whether this "
                "restaurant already has a shift with the same name.",
            )
        else:
            messages.success(
                request,
                "Shift updated successfully.",
            )
            return redirect("staff:shift_list")

    return render(
        request,
        "staff/shift_form.html",
        {
            "form": form,
            "shift": shift,
            "is_edit": True,
        },
    )


@login_required
@require_http_methods(["POST"])
def shift_set_status(request, shift_id):
    require_shift_manager(request.user)

    shifts = Shift.objects.select_related("restaurant")

    if not (
        request.user.is_superuser
        or request.user.role == "admin"
    ):
        shifts = shifts.filter(
            restaurant_id=request.user.restaurant_id
        )

    shift = get_object_or_404(shifts, pk=shift_id)
    action = request.POST.get("action", "").strip()

    if action not in ["activate", "deactivate"]:
        messages.error(request, "Invalid shift status action.")
        return redirect("staff:shift_list")

    if action == "deactivate":
        assigned_active_employees = shift.employees.filter(
            user__is_active=True,
            user__is_active_staff=True,
        ).count()

        if assigned_active_employees:
            messages.error(
                request,
                (
                    "This shift cannot be deactivated while "
                    f"{assigned_active_employees} active employee(s) "
                    "are assigned to it."
                ),
            )
            return redirect("staff:shift_list")

        shift.is_active = False
        message = "Shift deactivated successfully."
    else:
        shift.is_active = True
        message = "Shift activated successfully."

    shift.save(update_fields=["is_active"])
    messages.success(request, message)

    return redirect("staff:shift_list")


@login_required
@require_http_methods(["GET", "POST"])
def employee_assign_shift(request, employee_id):
    actor = request.user
    require_shift_manager(actor)

    employees = User.objects.filter(
        role__in=EMPLOYEE_ROLES,
        is_superuser=False,
    ).select_related("employee_profile", "restaurant")

    if not (actor.is_superuser or actor.role == "admin"):
        employees = employees.filter(
            restaurant_id=actor.restaurant_id
        )

        if actor.role == "manager":
            employees = employees.exclude(role="manager")

    employee = get_object_or_404(employees, pk=employee_id)
    profile = getattr(employee, "employee_profile", None)

    if profile is None:
        messages.info(
            request,
            "Complete the employee profile before assigning a shift.",
        )
        return redirect(
            "staff:employee_edit",
            employee_id=employee.pk,
        )

    if not employee.restaurant_id:
        raise PermissionDenied(
            "This employee is not assigned to a restaurant."
        )

    data = request.POST if request.method == "POST" else None
    form = EmployeeShiftForm(data=data, instance=profile)

    if request.method == "POST" and form.is_valid():
        profile = form.save(commit=False)
        profile.save(update_fields=["shift", "updated_at"])

        messages.success(request, "Employee shift updated.")
        return redirect(
            "staff:employee_detail",
            employee_id=employee.pk,
        )

    return render(
        request,
        "staff/employee_shift_form.html",
        {"form": form, "employee": employee},
    )

@login_required
@require_http_methods(["GET"])
def attendance_export_csv(request):
    if not (
        request.user.is_superuser
        or request.user.role in ["admin", "owner", "manager"]
    ):
        raise PermissionDenied(
            "You do not have permission to export attendance."
        )

    if not request.user.is_active or not request.user.is_active_staff:
        raise PermissionDenied("Your staff account is inactive.")

    local_now = timezone.localtime(
        timezone.now(),
        ATTENDANCE_TIMEZONE,
    )

    today = local_now.date()
    month_start = today.replace(day=1)

    report_from = (
        parse_date(request.GET.get("from_date", ""))
        or month_start
    )
    report_to = (
        parse_date(request.GET.get("to_date", ""))
        or today
    )

    if report_from > report_to:
        report_from, report_to = report_to, report_from

    employees = EmployeeProfile.objects.filter(
        user__role__in=EMPLOYEE_ROLES,
        user__is_superuser=False,
        user__is_active=True,
        user__is_active_staff=True,
    )

    if not (request.user.is_superuser or request.user.role == "admin"):
        if not request.user.restaurant_id:
            raise PermissionDenied(
                "Your account is not assigned to a restaurant."
            )

        employees = employees.filter(
            user__restaurant_id=request.user.restaurant_id
        )

    records = (
        Attendance.objects
        .filter(
            work_date__range=(report_from, report_to),
            employee__in=employees,
        )
        .select_related(
            "employee",
            "employee__user",
            "employee__user__restaurant",
            "shift",
        )
        .order_by("work_date", "check_in")
    )

    report_employee = request.GET.get(
        "report_employee",
        "",
    ).strip()

    if report_employee:
        try:
            report_employee_id = int(report_employee)
        except ValueError:
            report_employee_id = None

        if report_employee_id is not None:
            records = records.filter(
                employee_id=report_employee_id
            )

    filename = (
        f"attendance_{report_from.isoformat()}"
        f"_to_{report_to.isoformat()}.csv"
    )

    response = HttpResponse(
        content_type="text/csv; charset=utf-8",
    )
    response["Content-Disposition"] = (
        f'attachment; filename="{filename}"'
    )

    response.write("\ufeff")
    writer = csv.writer(response)

    writer.writerow(
        [
            "Date",
            "Employee ID",
            "Employee Name",
            "Role",
            "Restaurant",
            "Shift",
            "Scheduled Start",
            "Scheduled End",
            "Check In",
            "Check Out",
            "Worked Minutes",
            "Arrival Status",
        ]
    )

    for record in records:
        user = record.employee.user

        late_after = record.scheduled_start + timedelta(
            minutes=record.grace_minutes
        )

        arrival_status = (
            "Late"
            if record.check_in > late_after
            else "On time"
        )

        effective_checkout = record.check_out or local_now

        worked_minutes = max(
            0,
            int(
                (
                    effective_checkout - record.check_in
                ).total_seconds()
                // 60
            ),
        )

        check_in_local = timezone.localtime(
            record.check_in,
            ATTENDANCE_TIMEZONE,
        )

        check_out_local = (
            timezone.localtime(
                record.check_out,
                ATTENDANCE_TIMEZONE,
            )
            if record.check_out
            else None
        )

        scheduled_start_local = timezone.localtime(
            record.scheduled_start,
            ATTENDANCE_TIMEZONE,
        )
        scheduled_end_local = timezone.localtime(
            record.scheduled_end,
            ATTENDANCE_TIMEZONE,
        )

        writer.writerow(
            [
                record.work_date.isoformat(),
                record.employee.employee_id,
                user.get_full_name() or user.username,
                user.get_role_display(),
                user.restaurant.name if user.restaurant else "",
                record.shift.name,
                scheduled_start_local.strftime("%Y-%m-%d %H:%M"),
                scheduled_end_local.strftime("%Y-%m-%d %H:%M"),
                check_in_local.strftime("%Y-%m-%d %H:%M:%S"),
                (
                    check_out_local.strftime("%Y-%m-%d %H:%M:%S")
                    if check_out_local
                    else "Still working"
                ),
                worked_minutes,
                arrival_status,
            ]
        )

    return response


@login_required
@require_http_methods(["GET"])
def attendance_list(request):
    if not (
        request.user.is_superuser
        or request.user.role in ["admin", "owner", "manager"]
    ):
        raise PermissionDenied(
            "You do not have permission to manage attendance."
        )

    if not request.user.is_active or not request.user.is_active_staff:
        raise PermissionDenied("Your staff account is inactive.")

    local_now = timezone.localtime(
        timezone.now(),
        ATTENDANCE_TIMEZONE,
    )

    selected_date = (
        parse_date(request.GET.get("date", ""))
        or local_now.date()
    )

    employees = (
        EmployeeProfile.objects
        .filter(
            user__role__in=EMPLOYEE_ROLES,
            user__is_superuser=False,
            user__is_active=True,
            user__is_active_staff=True,
        )
        .select_related(
            "user",
            "user__restaurant",
            "shift",
        )
        .order_by(
            "user__first_name",
            "user__last_name",
            "user__username",
        )
    )

    if not (request.user.is_superuser or request.user.role == "admin"):
        if not request.user.restaurant_id:
            raise PermissionDenied(
                "Your account is not assigned to a restaurant."
            )

        employees = employees.filter(
            user__restaurant_id=request.user.restaurant_id
        )

    total_staff = employees.count()

    records = (
        Attendance.objects
        .filter(
            work_date=selected_date,
            employee__in=employees,
        )
        .select_related(
            "employee",
            "employee__user",
            "employee__user__restaurant",
            "shift",
        )
        .order_by("-check_in")
    )

    checked_in = records.count()
    still_working = records.filter(
        check_out__isnull=True
    ).count()

    late_count = 0

    for record in records:
        late_after = record.scheduled_start + timedelta(
            minutes=record.grace_minutes
        )

        record.arrival_status = (
            "Late"
            if record.check_in > late_after
            else "On time"
        )

        if record.arrival_status == "Late":
            late_count += 1

        effective_checkout = record.check_out or local_now

        total_minutes = max(
            0,
            int(
                (
                    effective_checkout - record.check_in
                ).total_seconds()
                // 60
            ),
        )

        hours, minutes = divmod(total_minutes, 60)
        record.worked_time = f"{hours}h {minutes}m"

    absent_count = max(total_staff - checked_in, 0)

    selected_employee = request.GET.get(
        "employee",
        "",
    ).strip()

    if selected_employee:
        try:
            selected_employee_id = int(selected_employee)
        except ValueError:
            selected_employee_id = None

        if selected_employee_id is not None:
            records = records.filter(
                employee_id=selected_employee_id
            )

    paginator = Paginator(records, 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    for record in page_obj:
        late_after = record.scheduled_start + timedelta(
            minutes=record.grace_minutes
        )

        record.arrival_status = (
            "Late"
            if record.check_in > late_after
            else "On time"
        )

        effective_checkout = record.check_out or local_now

        total_minutes = max(
            0,
            int(
                (
                    effective_checkout - record.check_in
                ).total_seconds()
                // 60
            ),
        )

        hours, minutes = divmod(total_minutes, 60)
        record.worked_time = f"{hours}h {minutes}m"

    today = local_now.date()
    yesterday = today - timedelta(days=1)
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)

    last_month_end = month_start - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)

    report_from = (
        parse_date(request.GET.get("from_date", ""))
        or month_start
    )
    report_to = (
        parse_date(request.GET.get("to_date", ""))
        or today
    )

    if report_from > report_to:
        report_from, report_to = report_to, report_from

    report_employee = request.GET.get(
        "report_employee",
        "",
    ).strip()

    report_records = (
        Attendance.objects
        .filter(
            work_date__range=(report_from, report_to),
            employee__in=employees,
        )
        .select_related(
            "employee",
            "employee__user",
            "employee__user__restaurant",
            "shift",
        )
        .order_by("-work_date", "-check_in")
    )

    if report_employee:
        try:
            report_employee_id = int(report_employee)
        except ValueError:
            report_employee_id = None

        if report_employee_id is not None:
            report_records = report_records.filter(
                employee_id=report_employee_id
            )

    report_records = list(report_records)

    report_late_count = 0
    report_total_minutes = 0

    for record in report_records:
        late_after = record.scheduled_start + timedelta(
            minutes=record.grace_minutes
        )

        record.arrival_status = (
            "Late"
            if record.check_in > late_after
            else "On time"
        )

        if record.arrival_status == "Late":
            report_late_count += 1

        effective_checkout = record.check_out or local_now

        worked_minutes = max(
            0,
            int(
                (
                    effective_checkout - record.check_in
                ).total_seconds()
                // 60
            ),
        )

        report_total_minutes += worked_minutes

        hours, minutes = divmod(worked_minutes, 60)
        record.worked_time = f"{hours}h {minutes}m"

    report_hours, report_minutes = divmod(
        report_total_minutes,
        60,
    )

    report_total_time = (
        f"{report_hours}h {report_minutes}m"
    )

    report_paginator = Paginator(report_records, 50)
    report_page_obj = report_paginator.get_page(
        request.GET.get("report_page")
    )

    return render(
        request,
        "staff/attendance_list.html",
        {
            "employees": employees,
            "page_obj": page_obj,
            "selected_date": selected_date,
            "selected_employee": selected_employee,
            "total_staff": total_staff,
            "checked_in": checked_in,
            "still_working": still_working,
            "late_count": late_count,
            "absent_count": absent_count,
            "report_page_obj": report_page_obj,
            "report_from": report_from,
            "report_to": report_to,
            "report_employee": report_employee,
            "report_record_count": len(report_records),
            "report_present_count": len(report_records),
            "report_late_count": report_late_count,
            "report_total_time": report_total_time,
            "today": today,
            "yesterday": yesterday,
            "week_start": week_start,
            "month_start": month_start,
            "last_month_start": last_month_start,
            "last_month_end": last_month_end,
            "year_start": year_start,
        },
    )

@login_required
@require_http_methods(["GET", "POST"])
def salary_advance_list(request):
    if not (
        request.user.is_superuser
        or request.user.role in ["admin", "owner", "manager"]
    ):
        raise PermissionDenied(
            "You do not have permission to manage salary advances."
        )

    if not request.user.is_active or not request.user.is_active_staff:
        raise PermissionDenied("Your staff account is inactive.")

    form = SalaryAdvanceForm(
        request.POST if request.method == "POST" else None,
        actor=request.user,
    )

    if request.method == "POST" and form.is_valid():
        salary_advance = form.save(commit=False)
        salary_advance.restaurant_id = (
            salary_advance.employee.user.restaurant_id
        )
        salary_advance.status = "pending"
        salary_advance.save()

        messages.success(
            request,
            "Salary advance request created successfully.",
        )
        return redirect("staff:salary_advance_list")

    advances = (
        SalaryAdvance.objects
        .select_related(
            "employee",
            "employee__user",
            "restaurant",
            "reviewed_by",
        )
        .order_by("-created_at")
    )

    if not (request.user.is_superuser or request.user.role == "admin"):
        if not request.user.restaurant_id:
            raise PermissionDenied(
                "Your account is not assigned to a restaurant."
            )

        advances = advances.filter(
            restaurant_id=request.user.restaurant_id
        )

    all_advances = advances

    total_advances = all_advances.count()
    pending_advances = all_advances.filter(
        status="pending"
    ).count()
    approved_advances = all_advances.filter(
        status="approved"
    ).count()
    deducted_advances = all_advances.filter(
        status="deducted"
    ).count()

    status_filter = request.GET.get("status", "").strip()
    search = request.GET.get("search", "").strip()

    if status_filter in [
        "pending",
        "approved",
        "rejected",
        "deducted",
    ]:
        advances = advances.filter(status=status_filter)

    if search:
        advances = advances.filter(
            Q(employee__employee_id__icontains=search)
            | Q(employee__user__first_name__icontains=search)
            | Q(employee__user__last_name__icontains=search)
            | Q(employee__user__username__icontains=search)
        )

    paginator = Paginator(advances, 25)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "staff/salary_advance_list.html",
        {
            "form": form,
            "page_obj": page_obj,
            "status_filter": status_filter,
            "search": search,
            "total_advances": total_advances,
            "pending_advances": pending_advances,
            "approved_advances": approved_advances,
            "deducted_advances": deducted_advances,
        },
    )


@login_required
@require_http_methods(["POST"])
def salary_advance_review(request, advance_id):
    if not (
        request.user.is_superuser
        or request.user.role in ["admin", "owner", "manager"]
    ):
        raise PermissionDenied(
            "You do not have permission to review salary advances."
        )

    if not request.user.is_active or not request.user.is_active_staff:
        raise PermissionDenied("Your staff account is inactive.")

    salary_advance = get_object_or_404(
        SalaryAdvance.objects.select_related(
            "employee",
            "employee__user",
        ),
        pk=advance_id,
    )

    if not (request.user.is_superuser or request.user.role == "admin"):
        if (
            not request.user.restaurant_id
            or salary_advance.restaurant_id
            != request.user.restaurant_id
        ):
            raise PermissionDenied(
                "You can only review advances from your restaurant."
            )

    action = request.POST.get("action", "").strip()
    review_note = request.POST.get("review_note", "").strip()

    if action not in ["approve", "reject"]:
        messages.error(
            request,
            "Invalid salary advance review action.",
        )
        return redirect("staff:salary_advance_list")

    if salary_advance.status != "pending":
        messages.error(
            request,
            "Only pending salary advances can be reviewed.",
        )
        return redirect("staff:salary_advance_list")

    salary_advance.status = (
        "approved"
        if action == "approve"
        else "rejected"
    )
    salary_advance.review_note = review_note
    salary_advance.reviewed_by = request.user
    salary_advance.reviewed_at = timezone.now()
    salary_advance.save(
        update_fields=[
            "status",
            "review_note",
            "reviewed_by",
            "reviewed_at",
            "updated_at",
        ]
    )

    messages.success(
        request,
        (
            "Salary advance approved successfully."
            if action == "approve"
            else "Salary advance rejected successfully."
        ),
    )

    return redirect("staff:salary_advance_list")


@login_required
@require_http_methods(["GET", "POST"])
def payroll_list(request):
    if not (
        request.user.is_superuser
        or request.user.role in ["admin", "owner", "manager"]
    ):
        raise PermissionDenied(
            "You do not have permission to manage payroll."
        )

    if not request.user.is_active or not request.user.is_active_staff:
        raise PermissionDenied("Your staff account is inactive.")

    restaurants = Restaurant.objects.order_by("name", "pk")

    selected_restaurant_id = (
        request.POST.get("restaurant")
        or request.GET.get("restaurant")
        or request.user.restaurant_id
    )

    if request.user.is_superuser or request.user.role == "admin":
        try:
            selected_restaurant_id = int(selected_restaurant_id)
        except (TypeError, ValueError):
            selected_restaurant_id = (
                restaurants.values_list("pk", flat=True).first()
            )

        selected_restaurant = get_object_or_404(
            restaurants,
            pk=selected_restaurant_id,
        )
    else:
        if not request.user.restaurant_id:
            raise PermissionDenied(
                "Your account is not assigned to a restaurant."
            )

        selected_restaurant_id = request.user.restaurant_id
        selected_restaurant = get_object_or_404(
            restaurants,
            pk=selected_restaurant_id,
        )

        restaurants = restaurants.filter(
            pk=selected_restaurant_id
        )

    selected_month_value = (
        request.POST.get("month")
        or request.GET.get("month")
        or timezone.localtime(
            timezone.now(),
            ATTENDANCE_TIMEZONE,
        ).strftime("%Y-%m")
    )

    try:
        selected_month = datetime.strptime(
            selected_month_value,
            "%Y-%m",
        ).date().replace(day=1)
    except ValueError:
        selected_month = timezone.localtime(
            timezone.now(),
            ATTENDANCE_TIMEZONE,
        ).date().replace(day=1)

        selected_month_value = selected_month.strftime("%Y-%m")

    if request.method == "POST":
        action = request.POST.get("action", "").strip()

        if action == "generate":
            result = generate_monthly_payroll(
                selected_restaurant_id,
                selected_month,
            )

            created_count = len(result["created"])
            existing_count = len(result["existing"])

            messages.success(
                request,
                (
                    f"{created_count} payroll record(s) created. "
                    f"{existing_count} existing record(s) skipped."
                ),
            )

            return redirect(
                f"{reverse('staff:payroll_list')}"
                f"?restaurant={selected_restaurant_id}"
                f"&month={selected_month_value}"
            )

        messages.error(request, "Invalid payroll action.")

    payroll_records = (
        PayrollRecord.objects
        .filter(
            restaurant_id=selected_restaurant_id,
            month=selected_month,
        )
        .select_related(
            "employee",
            "employee__user",
            "restaurant",
        )
        .order_by(
            "employee__user__first_name",
            "employee__user__last_name",
            "employee__employee_id",
        )
    )

    payroll_totals = payroll_records.aggregate(
        basic=Sum("basic_salary"),
        bonus=Sum("bonus"),
        deductions=(
            Sum("attendance_deduction")
            + Sum("leave_deduction")
            + Sum("advance_deduction")
        ),
        net=Sum("net_salary"),
    )

    paginator = Paginator(payroll_records, 25)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "staff/payroll_list.html",
        {
            "restaurants": restaurants,
            "selected_restaurant": selected_restaurant,
            "selected_restaurant_id": selected_restaurant_id,
            "selected_month": selected_month,
            "selected_month_value": selected_month_value,
            "page_obj": page_obj,
            "record_count": payroll_records.count(),
            "paid_count": payroll_records.filter(
                status="paid"
            ).count(),
            "draft_count": payroll_records.filter(
                status="draft"
            ).count(),
            "total_basic": payroll_totals["basic"] or 0,
            "total_bonus": payroll_totals["bonus"] or 0,
            "total_deductions": (
                payroll_totals["deductions"] or 0
            ),
            "total_net": payroll_totals["net"] or 0,
        },
    )


@login_required
@require_http_methods(["POST"])
def payroll_mark_paid(request, payroll_id):
    if not (
        request.user.is_superuser
        or request.user.role in ["admin", "owner", "manager"]
    ):
        raise PermissionDenied(
            "You do not have permission to update payroll."
        )

    if not request.user.is_active or not request.user.is_active_staff:
        raise PermissionDenied("Your staff account is inactive.")

    payroll_record = get_object_or_404(
        PayrollRecord.objects.select_related(
            "employee",
            "employee__user",
        ),
        pk=payroll_id,
    )

    if not (request.user.is_superuser or request.user.role == "admin"):
        if (
            not request.user.restaurant_id
            or payroll_record.restaurant_id
            != request.user.restaurant_id
        ):
            raise PermissionDenied(
                "You can only update payroll for your restaurant."
            )

    if payroll_record.status == "paid":
        messages.info(
            request,
            "This payroll record is already marked as paid.",
        )
    else:
        payroll_record.status = "paid"
        payroll_record.paid_at = timezone.now()
        payroll_record.save(
            update_fields=[
                "status",
                "paid_at",
                "updated_at",
            ]
        )

        messages.success(
            request,
            "Payroll marked as paid successfully.",
        )

    return redirect(
        f"{reverse('staff:payroll_list')}"
        f"?restaurant={payroll_record.restaurant_id}"
        f"&month={payroll_record.month:%Y-%m}"
    )
@login_required
@require_http_methods(["GET", "POST"])
def staff_leave_create(request):
    if not (
        request.user.is_superuser
        or request.user.role in ["admin", "owner"]
    ):
        raise PermissionDenied(
            "You do not have permission to create leave requests "
            "for staff."
        )

    if not request.user.is_active or not request.user.is_active_staff:
        raise PermissionDenied("Your staff account is inactive.")

    if (
        not request.user.is_superuser
        and request.user.role != "admin"
        and not request.user.restaurant_id
    ):
        raise PermissionDenied(
            "Your account is not assigned to a restaurant."
        )

    form = StaffLeaveRequestForm(
        request.POST if request.method == "POST" else None,
        actor=request.user,
    )

    if request.method == "POST" and form.is_valid():
        employee = form.cleaned_data["employee"]

        leave_request = form.save(commit=False)
        leave_request.employee = employee
        leave_request.restaurant_id = employee.user.restaurant_id
        leave_request.status = "pending"
        leave_request.save()

        messages.success(
            request,
            (
                "Leave request was created successfully for "
                f"{employee.user.get_full_name() or employee.user.username}."
            ),
        )
        return redirect("staff:leave_list")

    return render(
        request,
        "staff/staff_leave_form.html",
        {
            "form": form,
        },
    )

@login_required
@require_http_methods(["GET"])
def leave_list(request):
    if not (
        request.user.is_superuser
        or request.user.role in ["admin", "owner", "manager"]
    ):
        raise PermissionDenied(
            "You do not have permission to manage leave requests."
        )

    if not request.user.is_active or not request.user.is_active_staff:
        raise PermissionDenied("Your staff account is inactive.")

    leave_requests = (
        LeaveRequest.objects
        .select_related(
            "employee",
            "employee__user",
            "restaurant",
            "reviewed_by",
        )
        .order_by("-created_at")
    )

    if not (request.user.is_superuser or request.user.role == "admin"):
        if not request.user.restaurant_id:
            raise PermissionDenied(
                "Your account is not assigned to a restaurant."
            )

        leave_requests = leave_requests.filter(
            restaurant_id=request.user.restaurant_id
        )

    all_requests = leave_requests

    total_requests = all_requests.count()
    pending_requests = all_requests.filter(
        status="pending"
    ).count()
    approved_requests = all_requests.filter(
        status="approved"
    ).count()
    rejected_requests = all_requests.filter(
        status="rejected"
    ).count()

    status_filter = request.GET.get("status", "").strip()
    search = request.GET.get("search", "").strip()

    if status_filter in [
        "pending",
        "approved",
        "rejected",
        "cancelled",
    ]:
        leave_requests = leave_requests.filter(
            status=status_filter
        )

    if search:
        leave_requests = leave_requests.filter(
            Q(employee__employee_id__icontains=search)
            | Q(employee__user__first_name__icontains=search)
            | Q(employee__user__last_name__icontains=search)
            | Q(employee__user__username__icontains=search)
        )

    paginator = Paginator(leave_requests, 25)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "staff/leave_list.html",
        {
            "page_obj": page_obj,
            "status_filter": status_filter,
            "search": search,
            "total_requests": total_requests,
            "pending_requests": pending_requests,
            "approved_requests": approved_requests,
            "rejected_requests": rejected_requests,
        },
    )


@login_required
@require_http_methods(["POST"])
def leave_review(request, leave_id):
    if not (
        request.user.is_superuser
        or request.user.role in ["admin", "owner", "manager"]
    ):
        raise PermissionDenied(
            "You do not have permission to review leave requests."
        )

    if not request.user.is_active or not request.user.is_active_staff:
        raise PermissionDenied("Your staff account is inactive.")

    leave_request = get_object_or_404(
        LeaveRequest.objects.select_related(
            "employee",
            "employee__user",
        ),
        pk=leave_id,
    )

    if not (request.user.is_superuser or request.user.role == "admin"):
        if (
            not request.user.restaurant_id
            or leave_request.restaurant_id
            != request.user.restaurant_id
        ):
            raise PermissionDenied(
                "You can only review requests from your restaurant."
            )

    action = request.POST.get("action", "").strip()
    review_note = request.POST.get("review_note", "").strip()

    if action not in ["approve", "reject"]:
        messages.error(request, "Invalid leave review action.")
        return redirect("staff:leave_list")

    if leave_request.status != "pending":
        messages.error(
            request,
            "Only pending leave requests can be reviewed.",
        )
        return redirect("staff:leave_list")

    leave_request.status = (
        "approved"
        if action == "approve"
        else "rejected"
    )
    leave_request.review_note = review_note
    leave_request.reviewed_by = request.user
    leave_request.reviewed_at = timezone.now()
    leave_request.save(
        update_fields=[
            "status",
            "review_note",
            "reviewed_by",
            "reviewed_at",
            "updated_at",
        ]
    )

    messages.success(
        request,
        (
            "Leave request approved successfully."
            if action == "approve"
            else "Leave request rejected successfully."
        ),
    )

    return redirect("staff:leave_list")


@login_required
@require_http_methods(["GET", "POST"])
def my_leave(request):
    employee = None
    profile_error = ""
    page_obj = None

    try:
        employee = get_active_employee(request.user)
    except ValidationError as exc:
        profile_error = " ".join(exc.messages)

    form = LeaveRequestForm(
        request.POST if request.method == "POST" else None,
        employee=employee,
    )

    if request.method == "POST":
        if employee is None:
            messages.error(
                request,
                profile_error
                or "An active employee profile is required.",
            )
        elif not employee.user.restaurant_id:
            messages.error(
                request,
                "Your account is not assigned to a restaurant.",
            )
        elif form.is_valid():
            leave_request = form.save(commit=False)
            leave_request.employee = employee
            leave_request.restaurant_id = (
                employee.user.restaurant_id
            )
            leave_request.status = "pending"
            leave_request.save()

            messages.success(
                request,
                "Leave request submitted successfully.",
            )
            return redirect("staff:my_leave")

    pending_count = 0
    approved_count = 0
    approved_days = 0

    if employee is not None:
        leave_requests = (
            LeaveRequest.objects
            .filter(employee=employee)
            .select_related(
                "restaurant",
                "reviewed_by",
            )
            .order_by("-created_at")
        )

        pending_count = leave_requests.filter(
            status="pending"
        ).count()

        approved_requests = list(
            leave_requests.filter(status="approved")
        )
        approved_count = len(approved_requests)

        approved_days = sum(
            leave_request.total_days
            for leave_request in approved_requests
        )

        paginator = Paginator(leave_requests, 20)
        page_obj = paginator.get_page(
            request.GET.get("page")
        )

    return render(
        request,
        "staff/my_leave.html",
        {
            "employee": employee,
            "profile_error": profile_error,
            "form": form,
            "page_obj": page_obj,
            "pending_count": pending_count,
            "approved_count": approved_count,
            "approved_days": approved_days,
        },
    )


@login_required
@require_http_methods(["GET"])
def my_attendance(request):
    local_now = timezone.localtime(
        timezone.now(),
        ATTENDANCE_TIMEZONE,
    )
    local_today = local_now.date()

    can_manage_team = (
        request.user.is_superuser
        or request.user.role in ["admin", "owner", "manager"]
    )

    employees = (
        EmployeeProfile.objects
        .select_related("user", "shift", "user__restaurant")
        .filter(
            user__is_active=True,
            user__is_active_staff=True,
        )
        .order_by(
            "user__first_name",
            "user__last_name",
            "employee_id",
        )
    )

    if not (
        request.user.is_superuser
        or request.user.role == "admin"
    ):
        employees = employees.filter(
            user__restaurant_id=request.user.restaurant_id
        )

    own_employee = employees.filter(
        user=request.user
    ).first()

    selected_employee = None
    selected_employee_id = request.GET.get(
        "employee",
        "",
    ).strip()

    if selected_employee_id and can_manage_team:
        try:
            selected_employee_id = int(selected_employee_id)
        except ValueError:
            selected_employee_id = None

        if selected_employee_id is not None:
            selected_employee = get_object_or_404(
                employees,
                pk=selected_employee_id,
            )

    employee = selected_employee or own_employee
    viewing_employee = (
        selected_employee is not None
        and selected_employee.user_id != request.user.pk
    )

    today_records = (
        Attendance.objects
        .filter(
            employee__in=employees,
            work_date=local_today,
        )
        .select_related(
            "employee__user",
            "shift",
            "restaurant",
        )
    )

    total_staff = employees.count()
    checked_in_count = today_records.values(
        "employee_id"
    ).distinct().count()
    still_working_count = today_records.filter(
        check_out__isnull=True
    ).count()

    late_count = 0
    for attendance in today_records:
        late_after = (
            attendance.scheduled_start
            + timedelta(minutes=attendance.grace_minutes)
        )
        if attendance.check_in > late_after:
            late_count += 1

    absent_count = max(
        total_staff - checked_in_count,
        0,
    )

    recent_records = (
        Attendance.objects
        .filter(employee__in=employees)
        .select_related(
            "employee__user",
            "shift",
            "restaurant",
        )
        .order_by("-check_in")[:8]
    )

    for record in recent_records:
        if record.check_out:
            total_minutes = max(
                0,
                int(
                    (
                        record.check_out - record.check_in
                    ).total_seconds() // 60
                ),
            )
            hours, minutes = divmod(total_minutes, 60)
            record.worked_time = f"{hours}h {minutes}m"
        else:
            record.worked_time = "In progress"

    open_attendance = None
    page_obj = None

    if employee is not None:
        records = (
            Attendance.objects
            .filter(employee=employee)
            .select_related("shift")
            .order_by("-work_date", "-pk")
        )

        open_attendance = records.filter(
            check_out__isnull=True
        ).first()

        paginator = Paginator(records, 20)
        page_obj = paginator.get_page(
            request.GET.get("page")
        )

        for record in page_obj:
            delay_seconds = (
                record.check_in - record.scheduled_start
            ).total_seconds()

            record.arrival_status = (
                "Late"
                if delay_seconds > record.grace_minutes * 60
                else "On time"
            )

            if record.check_out is not None:
                total_minutes = max(
                    0,
                    int(
                        (
                            record.check_out - record.check_in
                        ).total_seconds() // 60
                    ),
                )
                hours, minutes = divmod(
                    total_minutes,
                    60,
                )
                record.worked_time = (
                    f"{hours}h {minutes}m"
                )
            else:
                record.worked_time = "In progress"

    show_owner_hub = (
        can_manage_team
        and own_employee is None
        and selected_employee is None
    )

    return render(
        request,
        "staff/my_attendance.html",
        {
            "employee": employee,
            "own_employee": own_employee,
            "employees": employees,
            "viewing_employee": viewing_employee,
            "show_owner_hub": show_owner_hub,
            "can_manage_team": can_manage_team,
            "open_attendance": open_attendance,
            "page_obj": page_obj,
            "local_now": local_now,
            "total_staff": total_staff,
            "checked_in_count": checked_in_count,
            "still_working_count": still_working_count,
            "late_count": late_count,
            "absent_count": absent_count,
            "recent_records": recent_records,
        },
    )

@login_required
@require_http_methods(["POST"])
def attendance_action(request):
    action = request.POST.get("action")

    if action not in ["check_in", "check_out"]:
        raise PermissionDenied("Invalid attendance action.")

    try:
        if action == "check_in":
            check_in_employee(request.user)
            messages.success(request, "Checked in successfully.")
        else:
            check_out_employee(request.user)
            messages.success(request, "Checked out successfully.")

    except ValidationError as exc:
        for error in exc.messages:
            messages.error(request, error)

    return redirect("staff:my_attendance")