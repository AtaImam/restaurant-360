from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.core.exceptions import PermissionDenied
from django.db import connection

from restaurant.models import Branch, Restaurant
from .models import (
    EmployeeProfile,
    LeaveRequest,
    SalaryAdvance,
    Shift,
)


User = get_user_model()


class EmployeeAccountForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = [
            "username",
            "first_name",
            "last_name",
            "email",
            "phone",
            "role",
            "branch",
            "restaurant",
            "password1",
            "password2",
        ]

    def __init__(self, *args, actor, branch=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.branch = branch

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
            raise PermissionDenied("You cannot manage employees.")

        allowed_roles = [
            "waiter",
            "chief",
            "kitchen_manager",
            "bar_manager",
        ]

        if actor.is_superuser or actor.role in ["admin", "owner"]:
            allowed_roles.insert(0, "manager")

        role_choices = [("", "Select Role...")] + [
            (value, label)
            for value, label in User.ROLE_CHOICES
            if value in allowed_roles
        ]
        self.fields["role"].choices = role_choices
        self.fields["role"].required = True
        if not self.is_bound and (not self.instance or not self.instance.pk):
            self.fields["role"].initial = ""
            self.initial["role"] = ""

        if branch is not None:
            self.instance.branch = branch
        self.fields["first_name"].required = True
        self.fields["restaurant"].required = True

        target_rest_id = branch.restaurant_id if branch is not None else actor.restaurant_id
        self.fields["restaurant"].queryset = (
            Restaurant.objects.filter(pk=target_rest_id).order_by("name", "pk")
        )

        if not (actor.is_superuser or actor.role == "admin"):
            if not actor.restaurant_id:
                raise PermissionDenied(
                    "Your account is not assigned to a restaurant."
                )

            self.fields["restaurant"].queryset = (
                Restaurant.objects.filter(pk=actor.restaurant_id)
            )
            self.fields["restaurant"].initial = actor.restaurant_id
            self.fields["restaurant"].disabled = True

        # Branch selection
        if target_rest_id:
            branch_qs = Branch.objects.filter(restaurant_id=target_rest_id)
            if actor.role == "manager":
                manager_branch = branch or getattr(actor, "branch", None)
                if manager_branch:
                    self.fields["branch"].queryset = branch_qs.filter(pk=manager_branch.pk)
                    self.fields["branch"].initial = manager_branch
                else:
                    self.fields["branch"].queryset = branch_qs
            else:
                self.fields["branch"].queryset = branch_qs.order_by("name", "pk")
                if branch is not None:
                    self.fields["branch"].initial = branch
        else:
            self.fields["branch"].queryset = Branch.objects.none()

        self.fields["branch"].required = False
        self.fields["branch"].empty_label = "Select Branch (Optional)"

    def clean_role(self):
        role = self.cleaned_data.get("role")
        if not role:
            raise forms.ValidationError("Please select a role.")
        if role in ["admin", "owner"]:
            raise forms.ValidationError("Cannot assign admin or owner roles.")
        if self.actor.role == "manager" and role == "manager":
            raise forms.ValidationError("Managers cannot assign the manager role.")
        return role

    def clean_branch(self):
        branch = self.cleaned_data.get("branch")
        if branch and self.actor.restaurant_id and branch.restaurant_id != self.actor.restaurant_id:
            raise forms.ValidationError("Selected branch does not belong to this restaurant.")
        if self.actor.role == "manager":
            allowed_branch = self.branch or getattr(self.actor, "branch", None)
            if allowed_branch and branch and branch != allowed_branch:
                raise forms.ValidationError("Managers can only assign staff to their own branch.")
        return branch


class EmployeeProfileForm(forms.ModelForm):
    class Meta:
        model = EmployeeProfile
        fields = [
            "employee_id",
            "joining_date",
            "basic_salary",
            "address",
            "emergency_contact_name",
            "emergency_contact_phone",
        ]

        widgets = {
            "joining_date": forms.DateInput(
                format="%Y-%m-%d",
                attrs={"type": "date"},
            ),
            "basic_salary": forms.NumberInput(
                attrs={"min": "0", "step": "0.01"},
            ),
            "address": forms.Textarea(
                attrs={"rows": 3},
            ),
        }

    def clean_employee_id(self):
        employee_id = self.cleaned_data["employee_id"].strip().upper()

        existing = EmployeeProfile.objects.filter(
            employee_id__iexact=employee_id
        )

        if self.instance.pk:
            existing = existing.exclude(pk=self.instance.pk)

        if existing.exists():
            raise forms.ValidationError(
                "This employee ID is already in use."
            )

        return employee_id

class EmployeeAccountEditForm(forms.ModelForm):
    class Meta:
        model = User
        fields = [
            "first_name",
            "last_name",
            "email",
            "phone",
            "role",
            "branch",
        ]

    def __init__(self, *args, actor, branch=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.branch = branch

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
            raise PermissionDenied("You cannot edit employees.")

        employee = self.instance

        if (
            not employee.pk
            or employee.is_superuser
            or employee.role not in [
                "manager",
                "waiter",
                "chief",
                "kitchen_manager",
                "bar_manager",
            ]
        ):
            raise PermissionDenied("This account cannot be edited here.")

        if not (actor.is_superuser or actor.role == "admin"):
            if (
                not actor.restaurant_id
                or employee.restaurant_id != actor.restaurant_id
            ):
                raise PermissionDenied(
                    "You can only edit employees in your restaurant."
                )

            if actor.role == "manager" and employee.role == "manager":
                raise PermissionDenied(
                    "Only an owner or administrator can edit managers."
                )

        self.fields["first_name"].required = True

        allowed_roles = [
            "waiter",
            "chief",
            "kitchen_manager",
            "bar_manager",
        ]
        if actor.is_superuser or actor.role in ["admin", "owner"]:
            allowed_roles.insert(0, "manager")

        self.fields["role"].choices = [
            (value, label)
            for value, label in User.ROLE_CHOICES
            if value in allowed_roles
        ]
        self.fields["role"].required = True
        if employee and employee.pk:
            self.initial["role"] = employee.role

        target_rest_id = employee.restaurant_id or (branch.restaurant_id if branch else actor.restaurant_id)
        if target_rest_id:
            branch_qs = Branch.objects.filter(restaurant_id=target_rest_id)
            if actor.role == "manager":
                manager_branch = branch or getattr(actor, "branch", None) or employee.branch
                if manager_branch:
                    self.fields["branch"].queryset = branch_qs.filter(pk=manager_branch.pk)
                    self.fields["branch"].initial = manager_branch
                else:
                    self.fields["branch"].queryset = branch_qs
            else:
                self.fields["branch"].queryset = branch_qs.order_by("name", "pk")
        else:
            self.fields["branch"].queryset = Branch.objects.none()

        self.fields["branch"].required = False
        self.fields["branch"].empty_label = "Select Branch (Optional)"
        if employee and employee.branch:
            self.initial["branch"] = employee.branch

    def clean_role(self):
        role = self.cleaned_data.get("role")
        if not role:
            raise forms.ValidationError("Please select a role.")
        if role in ["admin", "owner"]:
            raise forms.ValidationError("Cannot assign admin or owner roles.")
        if self.actor.role == "manager" and role == "manager":
            raise forms.ValidationError("Managers cannot assign the manager role.")
        if self.actor.role == "manager" and role not in ["waiter", "chief", "kitchen_manager", "bar_manager"]:
            raise forms.ValidationError("You do not have permission to assign this role.")
        return role

    def clean_branch(self):
        branch = self.cleaned_data.get("branch")
        if branch and self.actor.restaurant_id and branch.restaurant_id != self.actor.restaurant_id:
            raise forms.ValidationError("Selected branch does not belong to this restaurant.")
        if self.actor.role == "manager":
            allowed_branch = self.branch or getattr(self.actor, "branch", None) or self.instance.branch
            if allowed_branch and branch and branch != allowed_branch:
                raise forms.ValidationError("Managers can only assign staff to their own branch.")
        return branch
class ShiftForm(forms.ModelForm):
    class Meta:
        model = Shift
        fields = [
            "restaurant",
            "name",
            "start_time",
            "end_time",
            "grace_minutes",
        ]

        widgets = {
            "start_time": forms.TimeInput(
                format="%H:%M",
                attrs={"type": "time"},
            ),
            "end_time": forms.TimeInput(
                format="%H:%M",
                attrs={"type": "time"},
            ),
            "grace_minutes": forms.NumberInput(
                attrs={"min": "0", "max": "120"},
            ),
        }

        labels = {
            "name": "Shift name",
            "grace_minutes": "Late grace period (minutes)",
        }

    def __init__(self, *args, actor, branch=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.branch = branch or self.instance.branch
        self.instance.branch = self.branch

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

        self.fields["restaurant"].queryset = (
            Restaurant.objects.filter(pk=branch.restaurant_id if branch is not None else actor.restaurant_id).order_by("name", "pk")
        )

        if not (actor.is_superuser or actor.role == "admin"):
            if not actor.restaurant_id:
                raise PermissionDenied(
                    "Your account is not assigned to a restaurant."
                )

            self.fields["restaurant"].queryset = (
                Restaurant.objects.filter(pk=actor.restaurant_id)
            )
            self.fields["restaurant"].initial = actor.restaurant_id
            self.fields["restaurant"].disabled = True

    def clean(self):
        cleaned_data = super().clean()

        start = cleaned_data.get("start_time")
        end = cleaned_data.get("end_time")
        restaurant = cleaned_data.get("restaurant")
        name = cleaned_data.get("name")

        if start is not None and end is not None and start == end:
            self.add_error(
                "end_time",
                "Start time and end time must be different.",
            )

        if restaurant and name:
            existing = Shift.objects.filter(
                restaurant=restaurant,
                branch=self.branch,
                name__iexact=name,
            )

            if self.instance.pk:
                existing = existing.exclude(pk=self.instance.pk)

            if existing.exists():
                self.add_error(
                    "name",
                    "This branch already has a shift with this name.",
                )

        return cleaned_data

class EmployeeShiftForm(forms.ModelForm):
    class Meta:
        model = EmployeeProfile
        fields = ["shift"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["shift"].queryset = Shift.objects.filter(
            restaurant_id=self.instance.user.restaurant_id,
            branch_id=self.instance.user.branch_id,
            is_active=True,
        ).order_by("start_time", "name")

        self.fields["shift"].empty_label = "No shift assigned"
        self.fields["shift"].help_text = (
            "Only active shifts for this employee's restaurant are shown."
        )


class LeaveRequestForm(forms.ModelForm):
    class Meta:
        model = LeaveRequest
        fields = [
            "leave_type",
            "start_date",
            "end_date",
            "reason",
        ]

        widgets = {
            "start_date": forms.DateInput(
                format="%Y-%m-%d",
                attrs={"type": "date"},
            ),
            "end_date": forms.DateInput(
                format="%Y-%m-%d",
                attrs={"type": "date"},
            ),
            "reason": forms.Textarea(
                attrs={
                    "rows": 4,
                    "placeholder": (
                        "Explain the reason for your leave request."
                    ),
                },
            ),
        }

    def __init__(self, *args, employee=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.employee = employee

        self.fields["leave_type"].required = True
        self.fields["start_date"].required = True
        self.fields["end_date"].required = True
        self.fields["reason"].required = True

    def clean(self):
        cleaned_data = super().clean()

        start_date = cleaned_data.get("start_date")
        end_date = cleaned_data.get("end_date")
        if self.employee and start_date and start_date < self.employee.joining_date:
            self.add_error("start_date", "Leave cannot start before the employee joining date.")

        if (
            start_date is not None
            and end_date is not None
            and end_date < start_date
        ):
            self.add_error(
                "end_date",
                "End date cannot be before start date.",
            )

        if self.employee is not None and connection.in_atomic_block:
            EmployeeProfile.objects.select_for_update().get(pk=self.employee.pk)

        if (
            self.employee is not None
            and start_date is not None
            and end_date is not None
            and end_date >= start_date
        ):
            overlapping_requests = LeaveRequest.objects.filter(
                employee=self.employee,
                status__in=["pending", "approved"],
                start_date__lte=end_date,
                end_date__gte=start_date,
            )

            if self.instance.pk:
                overlapping_requests = overlapping_requests.exclude(
                    pk=self.instance.pk
                )

            if overlapping_requests.exists():
                raise forms.ValidationError(
                    "You already have a pending or approved leave "
                    "request that overlaps these dates."
                )

        return cleaned_data


class SalaryAdvanceForm(forms.ModelForm):
    class Meta:
        model = SalaryAdvance
        fields = [
            "employee",
            "amount",
            "reason",
        ]

        widgets = {
            "amount": forms.NumberInput(
                attrs={
                    "min": "0.01",
                    "step": "0.01",
                    "placeholder": "Advance amount",
                },
            ),
            "reason": forms.Textarea(
                attrs={
                    "rows": 4,
                    "placeholder": (
                        "Explain why the salary advance is needed."
                    ),
                },
            ),
        }

    def __init__(self, *args, actor, branch=None, **kwargs):
        super().__init__(*args, **kwargs)

        if (
            not actor.is_authenticated
            or not actor.is_active
            or not actor.is_active_staff
        ):
            raise PermissionDenied(
                "An active staff account is required."
            )

        if not (
            actor.is_superuser
            or actor.role in ["admin", "owner", "manager"]
        ):
            raise PermissionDenied(
                "You cannot manage salary advances."
            )

        employees = (
            EmployeeProfile.objects
            .filter(
                user__is_active=True,
                user__is_active_staff=True,
            )
            .select_related("user")
            .order_by(
                "user__first_name",
                "user__last_name",
                "employee_id",
            )
        )

        if not (actor.is_superuser or actor.role == "admin"):
            if not actor.restaurant_id:
                raise PermissionDenied(
                    "Your account is not assigned to a restaurant."
                )

            employees = employees.filter(
                user__restaurant_id=actor.restaurant_id
            )

        if branch is not None:
            employees = employees.filter(user__branch=branch)
        self.fields["employee"].queryset = employees
        self.fields["employee"].empty_label = "Select employee"

    def clean(self):
        cleaned_data = super().clean()

        employee = cleaned_data.get("employee")
        amount = cleaned_data.get("amount")

        if employee is not None and amount is not None:
            if connection.in_atomic_block:
                EmployeeProfile.objects.select_for_update().get(pk=employee.pk)
            basic_salary = employee.basic_salary or 0

            if amount > basic_salary:
                self.add_error(
                    "amount",
                    "Advance amount cannot exceed the employee's "
                    "current basic salary.",
                )

            existing_pending = SalaryAdvance.objects.filter(
                employee=employee,
                status="pending",
            )

            if self.instance.pk:
                existing_pending = existing_pending.exclude(
                    pk=self.instance.pk
                )

            if existing_pending.exists():
                raise forms.ValidationError(
                    "This employee already has a pending salary "
                    "advance request."
                )

        return cleaned_data

class StaffLeaveRequestForm(LeaveRequestForm):
    employee = forms.ModelChoiceField(
        queryset=EmployeeProfile.objects.none(),
        empty_label="Select an employee",
        required=True,
    )

    class Meta(LeaveRequestForm.Meta):
        fields = [
            "employee",
            "leave_type",
            "start_date",
            "end_date",
            "reason",
        ]

    def __init__(self, *args, actor=None, branch=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.actor = actor

        employees = (
            EmployeeProfile.objects
            .select_related("user", "user__restaurant")
            .filter(
                user__is_active=True,
                user__is_active_staff=True,
            )
            .exclude(user__is_superuser=True)
            .order_by(
                "user__first_name",
                "user__last_name",
                "employee_id",
            )
        )

        if actor is None or not actor.is_authenticated:
            employees = employees.none()
        elif not (actor.is_superuser or actor.role == "admin"):
            if actor.restaurant_id:
                employees = employees.filter(
                    user__restaurant_id=actor.restaurant_id
                )
            else:
                employees = employees.none()

        if branch is not None:
            employees = employees.filter(user__branch=branch)
        self.fields["employee"].queryset = employees

    def clean(self):
        employee = self.cleaned_data.get("employee")
        self.employee = employee

        cleaned_data = super().clean()

        if employee is None:
            return cleaned_data

        if not employee.user.restaurant_id:
            self.add_error(
                "employee",
                "The selected employee is not assigned to a restaurant.",
            )
            return cleaned_data

        actor = self.actor

        if (
            actor is not None
            and actor.is_authenticated
            and not (actor.is_superuser or actor.role == "admin")
            and employee.user.restaurant_id != actor.restaurant_id
        ):
            self.add_error(
                "employee",
                "You can only select an employee from your restaurant.",
            )
        return cleaned_data