from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.utils import timezone


class Shift(models.Model):
    restaurant = models.ForeignKey(
        "restaurant.Restaurant",
        on_delete=models.PROTECT,
        related_name="staff_shifts",
    )

    branch = models.ForeignKey(
        "restaurant.Branch",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="staff_shifts",
    )

    name = models.CharField(max_length=100)

    start_time = models.TimeField()
    end_time = models.TimeField()

    grace_minutes = models.PositiveSmallIntegerField(
        default=0,
        validators=[MaxValueValidator(120)],
        help_text="Allowed delay before attendance is marked late.",
    )

    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["restaurant_id", "start_time", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["branch", "name"],
                name="staff_unique_shift_name_per_branch",
            ),
            models.CheckConstraint(
                condition=~models.Q(
                    start_time=models.F("end_time")
                ),
                name="staff_shift_start_end_different",
            ),
            models.CheckConstraint(
                condition=models.Q(grace_minutes__lte=120),
                name="staff_shift_grace_max_120",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self.branch_id and self.restaurant_id:
            main_b = self.restaurant.branches.filter(is_main=True).first() or self.restaurant.branches.first()
            if main_b:
                self.branch = main_b
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.restaurant.name} - {self.name}"


class EmployeeProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="employee_profile",
    )

    employee_id = models.CharField(
        max_length=30,
        unique=True,
    )

    joining_date = models.DateField()

    shift = models.ForeignKey(
        "staff.Shift",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="employees",
    )

    address = models.TextField(blank=True)

    emergency_contact_name = models.CharField(
        max_length=150,
        blank=True,
    )

    emergency_contact_phone = models.CharField(
        max_length=20,
        blank=True,
    )

    basic_salary = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["employee_id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(basic_salary__gte=0),
                name="staff_employee_salary_nonnegative",
            ),
        ]

    def __str__(self):
        name = self.user.get_full_name() or self.user.username
        return f"{self.employee_id} - {name}"

class Attendance(models.Model):
    employee = models.ForeignKey(
        EmployeeProfile,
        on_delete=models.PROTECT,
        related_name="attendance_records",
    )

    restaurant = models.ForeignKey(
        "restaurant.Restaurant",
        on_delete=models.PROTECT,
        related_name="staff_attendance",
    )

    branch = models.ForeignKey(
        "restaurant.Branch",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="staff_attendance",
    )

    shift = models.ForeignKey(
        Shift,
        on_delete=models.PROTECT,
        related_name="attendance_records",
    )

    work_date = models.DateField()

    scheduled_start = models.DateTimeField()
    scheduled_end = models.DateTimeField()

    grace_minutes = models.PositiveSmallIntegerField(
        default=0,
        validators=[MaxValueValidator(120)],
    )

    check_in = models.DateTimeField()
    check_out = models.DateTimeField(
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-work_date", "-check_in"]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "work_date"],
                name="staff_one_attendance_per_work_date",
            ),
            models.UniqueConstraint(
                fields=["employee"],
                condition=models.Q(check_out__isnull=True),
                name="staff_one_open_attendance_per_employee",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    scheduled_end__gt=models.F("scheduled_start")
                ),
                name="staff_attendance_schedule_valid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(check_out__isnull=True)
                    | models.Q(check_out__gte=models.F("check_in"))
                ),
                name="staff_attendance_checkout_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(grace_minutes__lte=120),
                name="staff_attendance_grace_max_120",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self.branch_id:
            if self.shift_id and self.shift and self.shift.branch_id:
                self.branch_id = self.shift.branch_id
            elif self.employee_id and self.employee.user and self.employee.user.branch_id:
                self.branch_id = self.employee.user.branch_id
            elif self.restaurant_id:
                main_b = self.restaurant.branches.filter(is_main=True).first() or self.restaurant.branches.first()
                if main_b:
                    self.branch = main_b
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.employee.employee_id} - {self.work_date}"

    @property
    def is_late(self):
        late_after = self.scheduled_start + timedelta(minutes=self.grace_minutes)
        return self.check_in > late_after

    @property
    def late_minutes(self):
        late_after = self.scheduled_start + timedelta(minutes=self.grace_minutes)
        if self.check_in > late_after:
            return max(0, int((self.check_in - late_after).total_seconds() // 60))
        return 0

    @property
    def arrival_status(self):
        return "Late" if self.is_late else "On time"

    def get_effective_checkout(self, now=None):
        if self.check_out:
            return self.check_out
        return now or timezone.now()

    def get_worked_minutes(self, now=None):
        end_time = self.get_effective_checkout(now)
        if end_time < self.check_in:
            return 0
        return max(0, int((end_time - self.check_in).total_seconds() // 60))

    @property
    def worked_minutes(self):
        return self.get_worked_minutes()

    @property
    def worked_time(self):
        if not self.check_out:
            return "In progress"
        hours, minutes = divmod(self.worked_minutes, 60)
        return f"{hours}h {minutes}m"

    @property
    def scheduled_minutes(self):
        diff = (self.scheduled_end - self.scheduled_start).total_seconds()
        return max(0, int(diff // 60))

    @property
    def overtime_minutes(self):
        if not self.check_out:
            return 0
        return max(0, self.worked_minutes - self.scheduled_minutes)

class LeaveRequest(models.Model):
    LEAVE_TYPE_CHOICES = [
        ("annual", "Annual Leave"),
        ("sick", "Sick Leave"),
        ("casual", "Casual Leave"),
        ("emergency", "Emergency Leave"),
        ("unpaid", "Unpaid Leave"),
    ]

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
        ("cancelled", "Cancelled"),
    ]

    employee = models.ForeignKey(
        EmployeeProfile,
        on_delete=models.PROTECT,
        related_name="leave_requests",
    )

    restaurant = models.ForeignKey(
        "restaurant.Restaurant",
        on_delete=models.PROTECT,
        related_name="staff_leave_requests",
    )

    branch = models.ForeignKey(
        "restaurant.Branch",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="staff_leave_requests",
    )

    leave_type = models.CharField(
        max_length=20,
        choices=LEAVE_TYPE_CHOICES,
    )

    start_date = models.DateField()
    end_date = models.DateField()

    reason = models.TextField()

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending",
    )

    review_note = models.TextField(
        blank=True,
    )

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_staff_leave_requests",
    )

    reviewed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if not self.branch_id:
            if self.employee_id and self.employee.user and self.employee.user.branch_id:
                self.branch_id = self.employee.user.branch_id
            elif self.restaurant_id:
                main_b = self.restaurant.branches.filter(is_main=True).first() or self.restaurant.branches.first()
                if main_b:
                    self.branch = main_b
        super().save(*args, **kwargs)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_date__gte=models.F("start_date")),
                name="staff_leave_end_not_before_start",
            ),
        ]

    @property
    def total_days(self):
        return (self.end_date - self.start_date).days + 1

    def __str__(self):
        return (
            f"{self.employee.employee_id} - "
            f"{self.start_date} to {self.end_date}"
        )



class SalaryAdvance(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
        ("deducted", "Deducted"),
    ]

    employee = models.ForeignKey(
        EmployeeProfile,
        on_delete=models.PROTECT,
        related_name="salary_advances",
    )

    restaurant = models.ForeignKey(
        "restaurant.Restaurant",
        on_delete=models.PROTECT,
        related_name="staff_salary_advances",
    )

    branch = models.ForeignKey(
        "restaurant.Branch",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="staff_salary_advances",
    )

    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )

    reason = models.TextField()

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending",
    )

    review_note = models.TextField(blank=True)

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_salary_advances",
    )

    reviewed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    payroll_record = models.ForeignKey(
        "PayrollRecord",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="salary_advances",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if not self.branch_id:
            if self.employee_id and self.employee.user and self.employee.user.branch_id:
                self.branch_id = self.employee.user.branch_id
            elif self.restaurant_id:
                main_b = self.restaurant.branches.filter(is_main=True).first() or self.restaurant.branches.first()
                if main_b:
                    self.branch = main_b
        super().save(*args, **kwargs)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="staff_salary_advance_amount_positive",
            ),
        ]

    def __str__(self):
        return (
            f"{self.employee.employee_id} - "
            f"{self.amount} ({self.status})"
        )


class PayrollRecord(models.Model):
    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("paid", "Paid"),
    ]

    employee = models.ForeignKey(
        EmployeeProfile,
        on_delete=models.PROTECT,
        related_name="payroll_records",
    )

    restaurant = models.ForeignKey(
        "restaurant.Restaurant",
        on_delete=models.PROTECT,
        related_name="staff_payroll_records",
    )

    branch = models.ForeignKey(
        "restaurant.Branch",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="staff_payroll_records",
    )

    month = models.DateField(
        help_text="Use the first day of the payroll month.",
    )

    basic_salary = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    bonus = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    attendance_deduction = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    leave_deduction = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    advance_deduction = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    net_salary = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="draft",
    )

    paid_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    note = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if not self.branch_id:
            if self.employee_id and self.employee.user and self.employee.user.branch_id:
                self.branch_id = self.employee.user.branch_id
            elif self.restaurant_id:
                main_b = self.restaurant.branches.filter(is_main=True).first() or self.restaurant.branches.first()
                if main_b:
                    self.branch = main_b
        super().save(*args, **kwargs)

    class Meta:
        ordering = ["-month", "employee__employee_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "month"],
                name="staff_unique_employee_payroll_month",
            ),
            models.CheckConstraint(
                condition=models.Q(basic_salary__gte=0),
                name="staff_payroll_basic_salary_nonnegative",
            ),
            models.CheckConstraint(
                condition=models.Q(bonus__gte=0),
                name="staff_payroll_bonus_nonnegative",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    attendance_deduction__gte=0
                ),
                name="staff_payroll_attendance_deduction_nonnegative",
            ),
            models.CheckConstraint(
                condition=models.Q(leave_deduction__gte=0),
                name="staff_payroll_leave_deduction_nonnegative",
            ),
            models.CheckConstraint(
                condition=models.Q(advance_deduction__gte=0),
                name="staff_payroll_advance_deduction_nonnegative",
            ),
            models.CheckConstraint(
                condition=models.Q(net_salary__gte=0),
                name="staff_payroll_net_salary_nonnegative",
            ),
        ]

    def calculate_net_salary(self):
        calculated_salary = (
            self.basic_salary
            + self.bonus
            - self.attendance_deduction
            - self.leave_deduction
            - self.advance_deduction
        )

        return max(calculated_salary, Decimal("0.00"))

    def __str__(self):
        return (
            f"{self.employee.employee_id} - "
            f"{self.month:%B %Y}"
        )



class DailyTableAssignment(models.Model):
    work_date = models.DateField()

    restaurant = models.ForeignKey(
        "restaurant.Restaurant",
        on_delete=models.PROTECT,
        related_name="daily_table_assignments",
    )

    branch = models.ForeignKey(
        "restaurant.Branch",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="daily_table_assignments",
    )

    table = models.ForeignKey(
        "restaurant.Table",
        on_delete=models.PROTECT,
        related_name="daily_waiter_assignments",
    )

    waiter = models.ForeignKey(
        EmployeeProfile,
        on_delete=models.PROTECT,
        related_name="daily_table_assignments",
    )

    attendance = models.ForeignKey(
        Attendance,
        on_delete=models.PROTECT,
        related_name="table_assignments",
    )

    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="assigned_daily_tables",
    )

    is_active = models.BooleanField(default=True)

    assigned_at = models.DateTimeField(auto_now_add=True)

    ended_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    def save(self, *args, **kwargs):
        if not self.branch_id:
            if self.table_id and self.table and self.table.branch_id:
                self.branch_id = self.table.branch_id
            elif self.waiter_id and self.waiter.user and self.waiter.user.branch_id:
                self.branch_id = self.waiter.user.branch_id
            elif self.restaurant_id:
                main_b = self.restaurant.branches.filter(is_main=True).first() or self.restaurant.branches.first()
                if main_b:
                    self.branch = main_b
        super().save(*args, **kwargs)

    class Meta:
        ordering = [
            "-work_date",
            "table__table_number",
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["table", "work_date"],
                condition=models.Q(is_active=True),
                name="staff_one_waiter_per_table_day",
            ),
        ]

    def __str__(self):
        return (
            f"{self.work_date} - Table "
            f"{self.table.table_number} - "
            f"{self.waiter.employee_id}"
        )


class StaffTask(models.Model):
    restaurant = models.ForeignKey(
        "restaurant.Restaurant",
        on_delete=models.PROTECT,
        related_name="staff_tasks",
    )

    branch = models.ForeignKey(
        "restaurant.Branch",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="staff_tasks",
    )

    work_date = models.DateField()

    employee = models.ForeignKey(
        EmployeeProfile,
        on_delete=models.PROTECT,
        related_name="assigned_tasks",
    )

    attendance = models.ForeignKey(
        Attendance,
        on_delete=models.PROTECT,
        related_name="assigned_tasks",
    )

    related_order = models.ForeignKey(
        "orders.Order",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="staff_tasks",
    )

    related_table = models.ForeignKey(
        "restaurant.Table",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="staff_tasks",
    )

    title = models.CharField(max_length=200)

    instructions = models.TextField(blank=True)

    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_staff_tasks",
    )

    is_completed = models.BooleanField(default=False)

    assigned_at = models.DateTimeField(auto_now_add=True)

    completed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    def save(self, *args, **kwargs):
        if not self.branch_id:
            if self.employee_id and self.employee.user and self.employee.user.branch_id:
                self.branch_id = self.employee.user.branch_id
            elif self.restaurant_id:
                main_b = self.restaurant.branches.filter(is_main=True).first() or self.restaurant.branches.first()
                if main_b:
                    self.branch = main_b
        super().save(*args, **kwargs)

    class Meta:
        ordering = [
            "is_completed",
            "-assigned_at",
        ]
        indexes = [
            models.Index(
                fields=[
                    "restaurant",
                    "work_date",
                    "is_completed",
                ],
                name="staff_task_daily_idx",
            ),
        ]

    def __str__(self):
        return (
            f"{self.employee.employee_id} - "
            f"{self.title}"
        )


class OrderStaffService(models.Model):
    order = models.OneToOneField(
        "orders.Order",
        on_delete=models.CASCADE,
        related_name="staff_service",
    )

    waiter = models.ForeignKey(
        EmployeeProfile,
        on_delete=models.PROTECT,
        related_name="served_order_records",
    )

    table_assignment = models.ForeignKey(
        DailyTableAssignment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="order_services",
    )

    assigned_at = models.DateTimeField(auto_now_add=True)

    ready_at = models.DateTimeField(null=True, blank=True)

    served_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    completed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    order_taken_by = models.ForeignKey(
        EmployeeProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orders_taken_services",
        help_text="Staff member who took/entered the order.",
    )

    served_by = models.ForeignKey(
        EmployeeProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orders_served_services",
        help_text="Staff member who marked the order as served.",
    )

    payment_handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments_handled_services",
        help_text="User who processed or recorded payment.",
    )

    handover_by = models.ForeignKey(
        EmployeeProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="handover_from_services",
        help_text="Previous waiter if service was handed over.",
    )

    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="closed_order_services",
        help_text="User who closed or cleaned the table service.",
    )

    class Meta:
        ordering = ["-assigned_at"]

    def __str__(self):
        return (
            f"Order #{self.order_id} - "
            f"{self.waiter.employee_id}"
        )


class StaffNotification(models.Model):
    TYPE_CHOICES = [
        ("new_order", "New Order"),
        ("food_ready", "Food Ready"),
        ("order_served", "Order Served"),
        ("order_completed", "Order Completed"),
        ("task_assigned", "Task Assigned"),
        ("general", "General"),
    ]

    restaurant = models.ForeignKey(
        "restaurant.Restaurant",
        on_delete=models.CASCADE,
        related_name="staff_notifications",
    )

    branch = models.ForeignKey(
        "restaurant.Branch",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="staff_notifications",
    )

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="staff_notifications",
    )

    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="staff_notifications",
    )

    notification_type = models.CharField(
        max_length=30,
        choices=TYPE_CHOICES,
    )

    title = models.CharField(max_length=200)

    message = models.TextField()

    event_key = models.CharField(
        max_length=180,
        help_text=(
            "Stable event identifier used to prevent "
            "duplicate notifications."
        ),
    )

    is_read = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    read_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    def save(self, *args, **kwargs):
        if not self.branch_id:
            if self.recipient_id and hasattr(self.recipient, "branch_id") and self.recipient.branch_id:
                self.branch_id = self.recipient.branch_id
            elif self.order_id and self.order and self.order.branch_id:
                self.branch_id = self.order.branch_id
            elif self.restaurant_id:
                main_b = self.restaurant.branches.filter(is_main=True).first() or self.restaurant.branches.first()
                if main_b:
                    self.branch = main_b
        super().save(*args, **kwargs)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["recipient", "event_key"],
                name="staff_unique_recipient_event",
            ),
        ]
        indexes = [
            models.Index(
                fields=["recipient", "is_read"],
                name="staff_notif_user_read_idx",
            ),
            models.Index(
                fields=["order"],
                name="staff_notif_order_idx",
            ),
        ]

    def __str__(self):
        return (
            f"{self.recipient} - "
            f"{self.title}"
        )
