from django import forms

from orders.models import Order
from restaurant.models import Table

from .models import Attendance


class TableAssignmentForm(forms.Form):
    attendance = forms.ModelChoiceField(
        queryset=Attendance.objects.none(),
        label="Present waiter",
        empty_label="Select a waiter",
    )

    table = forms.ModelChoiceField(
        queryset=Table.objects.none(),
        label="Table",
        empty_label="Select a table",
    )

    def __init__(
        self,
        *args,
        restaurant,
        work_date,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self.fields["attendance"].queryset = (
            Attendance.objects
            .filter(
                restaurant=restaurant,
                work_date=work_date,
                check_out__isnull=True,
                employee__user__role="waiter",
                employee__user__is_active=True,
                employee__user__is_active_staff=True,
            )
            .select_related(
                "employee__user",
                "employee__shift",
            )
            .order_by(
                "employee__user__first_name",
                "employee__employee_id",
            )
        )

        self.fields["table"].queryset = (
            Table.objects
            .filter(
                restaurant=restaurant,
                is_active=True,
            )
            .order_by("table_number")
        )

        self.fields[
            "attendance"
        ].label_from_instance = (
            lambda attendance: (
                f"{attendance.employee.employee_id} - "
                f"{attendance.employee.user.get_full_name() or attendance.employee.user.username}"
            )
        )

        self.fields["table"].label_from_instance = (
            lambda table: f"Table {table.table_number}"
        )


class StaffTaskAssignmentForm(forms.Form):
    attendance = forms.ModelChoiceField(
        queryset=Attendance.objects.none(),
        label="Present employee",
        empty_label="Select an employee",
    )

    title = forms.CharField(
        max_length=200,
        label="Task",
        widget=forms.TextInput(
            attrs={
                "placeholder": (
                    "Example: Prepare ingredients for dinner"
                ),
            }
        ),
    )

    instructions = forms.CharField(
        required=False,
        label="Instructions",
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": (
                    "Optional instructions for the employee"
                ),
            }
        ),
    )

    related_order = forms.ModelChoiceField(
        queryset=Order.objects.none(),
        required=False,
        label="Related order",
        empty_label="No related order",
    )

    related_table = forms.ModelChoiceField(
        queryset=Table.objects.none(),
        required=False,
        label="Related table",
        empty_label="No related table",
    )

    def __init__(
        self,
        *args,
        restaurant,
        work_date,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self.fields["attendance"].queryset = (
            Attendance.objects
            .filter(
                restaurant=restaurant,
                work_date=work_date,
                check_out__isnull=True,
                employee__user__is_active=True,
                employee__user__is_active_staff=True,
            )
            .select_related(
                "employee__user",
                "employee__shift",
            )
            .order_by(
                "employee__user__role",
                "employee__user__first_name",
                "employee__employee_id",
            )
        )

        self.fields["related_order"].queryset = (
            Order.objects
            .filter(restaurant=restaurant)
            .exclude(status="COMPLETED")
            .select_related("table")
            .order_by("-created_at")
        )

        self.fields["related_table"].queryset = (
            Table.objects
            .filter(
                restaurant=restaurant,
                is_active=True,
            )
            .order_by("table_number")
        )

        self.fields[
            "attendance"
        ].label_from_instance = (
            lambda attendance: (
                f"{attendance.employee.employee_id} - "
                f"{attendance.employee.user.get_full_name() or attendance.employee.user.username} "
                f"({attendance.employee.user.get_role_display()})"
            )
        )

        self.fields[
            "related_order"
        ].label_from_instance = (
            lambda order: (
                f"Order #{order.pk} - "
                f"{'Table ' + str(order.table.table_number) if order.table else 'Takeaway'}"
            )
        )

        self.fields[
            "related_table"
        ].label_from_instance = (
            lambda table: f"Table {table.table_number}"
        )