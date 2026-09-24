from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from menu.models import MenuItem
from restaurant.models import Branch, Restaurant, Table


class TableSession(models.Model):
    STATUS_OPEN = "OPEN"
    STATUS_CLOSED = "CLOSED"

    STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_CLOSED, "Closed"),
    ]

    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        related_name="table_sessions",
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="table_sessions",
    )
    table = models.ForeignKey(
        Table,
        on_delete=models.CASCADE,
        related_name="sessions",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_OPEN,
        db_index=True,
    )
    opened_at = models.DateTimeField(
        default=timezone.now,
    )
    closed_at = models.DateTimeField(
        null=True,
        blank=True,
    )
    clean_needed = models.BooleanField(default=False, db_index=True)
    cleaned_at = models.DateTimeField(null=True, blank=True)
    cleaned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="cleaned_table_sessions",
    )

    notes = models.TextField(
        blank=True,
        default="",
    )

    class Meta:
        ordering = ["-opened_at"]

    @property
    def is_open(self):
        return self.status == self.STATUS_OPEN

    @property
    def is_closed(self):
        return self.status == self.STATUS_CLOSED

    def close(self, commit=True):
        self.status = self.STATUS_CLOSED
        self.closed_at = timezone.now()
        if commit:
            self.save(update_fields=["status", "closed_at"])

    @property
    def total_amount(self):
        orders = self.orders.exclude(status="CANCELLED")
        return sum((o.total_amount for o in orders), Decimal("0.00"))

    @property
    def paid_amount(self):
        orders = self.orders.exclude(status="CANCELLED").filter(payment_status="PAID")
        return sum((o.total_amount for o in orders), Decimal("0.00"))

    @property
    def unpaid_amount(self):
        orders = self.orders.exclude(status="CANCELLED").exclude(payment_status="PAID")
        return sum((o.total_amount for o in orders), Decimal("0.00"))

    @property
    def is_fully_paid(self):
        return self.unpaid_amount == Decimal("0.00")

    @property
    def has_unpaid_orders(self):
        return self.orders.exclude(status__in=["COMPLETED", "CANCELLED"]).filter(payment_status="UNPAID").exists()

    def save(self, *args, **kwargs):
        if not self.branch_id:
            if self.table_id and self.table and self.table.branch_id:
                self.branch_id = self.table.branch_id
            elif self.restaurant_id:
                main_b = self.restaurant.branches.filter(is_main=True).first() or self.restaurant.branches.first()
                if main_b:
                    self.branch = main_b
        super().save(*args, **kwargs)

    def __str__(self):
        table_num = self.table.table_number if self.table else "?"
        return f"Session #{self.id} - Table {table_num} ({self.get_status_display()})"


DiningSession = TableSession


class Coupon(models.Model):
    DISCOUNT_TYPE_PERCENTAGE = "PERCENTAGE"
    DISCOUNT_TYPE_FIXED = "FIXED"
    DISCOUNT_TYPE_CHOICES = [
        (DISCOUNT_TYPE_PERCENTAGE, "Percentage (%)"),
        (DISCOUNT_TYPE_FIXED, "Fixed Amount (৳)"),
    ]

    APPLICABILITY_WHOLE_ORDER = "WHOLE_ORDER"
    APPLICABILITY_CATEGORY = "CATEGORY"
    APPLICABILITY_MENU_ITEM = "MENU_ITEM"
    APPLICABILITY_CHOICES = [
        (APPLICABILITY_WHOLE_ORDER, "Whole Order"),
        (APPLICABILITY_CATEGORY, "Selected Categories"),
        (APPLICABILITY_MENU_ITEM, "Selected Menu Items"),
    ]

    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        related_name="coupons",
    )
    name = models.CharField(max_length=150)
    code = models.CharField(
        max_length=50,
        blank=True,
        default="",
        db_index=True,
        help_text="Coupon code (case-insensitive). Leave blank for automatic offers.",
    )
    is_automatic = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Automatically applied to qualifying orders if no coupon code is specified.",
    )
    discount_type = models.CharField(
        max_length=20,
        choices=DISCOUNT_TYPE_CHOICES,
        default=DISCOUNT_TYPE_PERCENTAGE,
    )
    discount_value = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text="Percentage value (e.g. 10.00 for 10%) or Fixed amount (e.g. 50.00 for ৳50).",
    )
    max_discount_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Maximum discount cap for percentage offers (null for uncapped).",
    )
    min_order_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Minimum eligible subtotal required to use this coupon.",
    )
    start_datetime = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Optional start date/time when coupon becomes active.",
    )
    end_datetime = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Optional expiry date/time after which coupon cannot be used.",
    )
    usage_limit = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Maximum total times this coupon can be used across all orders (null for unlimited).",
    )
    times_used = models.PositiveIntegerField(
        default=0,
        help_text="Count of successful orders that used this coupon.",
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
    )
    applicability = models.CharField(
        max_length=20,
        choices=APPLICABILITY_CHOICES,
        default=APPLICABILITY_WHOLE_ORDER,
    )
    applicable_categories = models.ManyToManyField(
        "menu.Category",
        blank=True,
        related_name="applicable_coupons",
    )
    applicable_items = models.ManyToManyField(
        "menu.MenuItem",
        blank=True,
        related_name="applicable_coupons",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["restaurant", "code"],
                condition=models.Q(code__gt=""),
                name="unique_coupon_code_per_restaurant",
            )
        ]

    def clean(self):
        super().clean()
        if self.code:
            self.code = self.code.strip().upper()
        if self.discount_value is not None and self.discount_value <= Decimal("0.00"):
            from django.core.exceptions import ValidationError
            raise ValidationError({"discount_value": "Discount value must be greater than zero."})
        if self.discount_type == self.DISCOUNT_TYPE_PERCENTAGE and self.discount_value and self.discount_value > Decimal("100.00"):
            from django.core.exceptions import ValidationError
            raise ValidationError({"discount_value": "Percentage discount cannot exceed 100%."})
        if self.start_datetime and self.end_datetime and self.start_datetime >= self.end_datetime:
            from django.core.exceptions import ValidationError
            raise ValidationError({"end_datetime": "End date/time must be strictly after start date/time."})

    def save(self, *args, **kwargs):
        if self.code:
            self.code = self.code.strip().upper()
        super().save(*args, **kwargs)

    @property
    def is_currently_valid(self):
        if not self.is_active:
            return False
        now = timezone.now()
        if self.start_datetime and now < self.start_datetime:
            return False
        if self.end_datetime and now > self.end_datetime:
            return False
        if self.usage_limit is not None and self.times_used >= self.usage_limit:
            return False
        return True

    def __str__(self):
        code_str = f" [{self.code}]" if self.code else " (Automatic)"
        return f"{self.name}{code_str} - {self.get_discount_type_display()}"



class Order(models.Model):

    ORDER_TYPES = [
        ("DINE_IN", "Dine In"),
        ("TAKEAWAY", "Takeaway"),
    ]

    STATUS_NEW = "NEW"
    STATUS_ACCEPTED = "ACCEPTED"
    STATUS_PREPARING = "PREPARING"
    STATUS_READY = "READY"
    STATUS_SERVED = "SERVED"
    STATUS_COMPLETED = "COMPLETED"
    STATUS_CANCELLED = "CANCELLED"

    STATUS_CHOICES = [
        (STATUS_NEW, "New"),
        (STATUS_ACCEPTED, "Accepted"),
        (STATUS_PREPARING, "Preparing"),
        (STATUS_READY, "Ready"),
        (STATUS_SERVED, "Served"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    PAYMENT_STATUS_CHOICES = [
        ("UNPAID", "Unpaid"),
        ("PAID", "Paid"),
        ("REFUNDED", "Refunded"),
    ]

    PAYMENT_METHOD_CHOICES = [
        ("CASH", "Cash"),
        ("CARD", "Card"),
        ("MOBILE_BANKING", "Mobile Banking"),
    ]

    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        related_name="orders",
    )

    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="orders",
    )

    table = models.ForeignKey(
        Table,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    table_session = models.ForeignKey(
        TableSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orders",
    )

    order_type = models.CharField(
        max_length=20,
        choices=ORDER_TYPES,
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="NEW",
    )

    subtotal = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
    )

    discount_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
    )

    applied_coupon = models.ForeignKey(
        Coupon,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orders",
    )

    coupon_code_snapshot = models.CharField(
        max_length=50,
        blank=True,
        default="",
    )

    coupon_name_snapshot = models.CharField(
        max_length=150,
        blank=True,
        default="",
    )

    discount_type_snapshot = models.CharField(
        max_length=20,
        blank=True,
        default="",
    )

    discount_rate_snapshot = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )

    service_charge = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
    )

    vat_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
    )

    total_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
    )

    payment_status = models.CharField(
        max_length=20,
        choices=PAYMENT_STATUS_CHOICES,
        default="UNPAID",
    )

    payment_method = models.CharField(
        max_length=30,
        choices=PAYMENT_METHOD_CHOICES,
        blank=True,
        default="",
    )

    payment_reference = models.CharField(
        max_length=100,
        blank=True,
        default="",
    )

    paid_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
    )

    refund_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    settings_snapshot = models.JSONField(default=dict, blank=True, editable=False)

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    status_changed_at = models.DateTimeField(null=True, blank=True)

    @property
    def session(self):
        return self.table_session

    @property
    def dining_session(self):
        return self.table_session

    @transaction.atomic
    def save(self, *args, **kwargs):
        if self.payment_status == "PAID" and not self.paid_at:
            self.paid_at = timezone.now()
        elif self.payment_status == "UNPAID":
            self.paid_at = None

        if not self.branch_id:
            if self.table_id and self.table and self.table.branch_id:
                self.branch_id = self.table.branch_id
            elif self.table_session_id and self.table_session and self.table_session.branch_id:
                self.branch_id = self.table_session.branch_id
            elif self.restaurant_id:
                main_b = self.restaurant.branches.filter(is_main=True).first() or self.restaurant.branches.first()
                if main_b:
                    self.branch = main_b

        if self.order_type == "DINE_IN" and self.table_id:
            self.table = Table.objects.select_for_update().get(pk=self.table_id)
            if not self.table_session_id:
                from orders.services import get_live_order_cutoff
                live_cutoff = get_live_order_cutoff()
                open_session = TableSession.objects.filter(
                    table_id=self.table_id,
                    status=TableSession.STATUS_OPEN,
                    opened_at__gte=live_cutoff,
                ).order_by("-opened_at").first()
                if not open_session:
                    open_session = TableSession.objects.create(
                        restaurant_id=self.restaurant_id,
                        branch_id=self.branch_id,
                        table_id=self.table_id,
                        status=TableSession.STATUS_OPEN,
                    )
                self.table_session = open_session
            if self._state.adding and self.table and self.table.status == Table.STATUS_AVAILABLE:
                self.table.mark_occupied()
        elif self.order_type != "DINE_IN":
            self.table_session = None
        if self._state.adding and not self.settings_snapshot:
            from business_settings.services import order_snapshot
            self.settings_snapshot = order_snapshot(self.restaurant, self.branch, applied_rates=False)
        super().save(*args, **kwargs)
        if self.table_session_id:
            from orders.services import release_settled_table_session
            release_settled_table_session(self.table_session_id)

    def __str__(self):
        return f"Order #{self.id}"


class OrderItem(models.Model):

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="items",
    )

    menu_item = models.ForeignKey(
        MenuItem,
        on_delete=models.PROTECT,
    )

    quantity = models.PositiveIntegerField()

    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
    )

    discount_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    @property
    def addons_total(self):
        return sum((a.price for a in self.addons.all()), Decimal("0.00"))

    @property
    def unit_price_with_addons(self):
        return self.price + self.addons_total

    @property
    def subtotal(self):
        return self.unit_price_with_addons * self.quantity

    @property
    def net_subtotal(self):
        return max(Decimal("0.00"), self.subtotal - self.discount_amount)

    def __str__(self):
        return f"{self.menu_item.name} x {self.quantity}"


class OrderItemAddon(models.Model):
    order_item = models.ForeignKey(
        OrderItem,
        on_delete=models.CASCADE,
        related_name="addons",
    )
    addon_option = models.ForeignKey(
        "menu.AddonOption",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="order_item_addons",
    )
    addon_group_name = models.CharField(
        max_length=120,
        blank=True,
        default="",
    )
    addon_name = models.CharField(
        max_length=150,
    )
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.order_item_id} - {self.addon_name} (৳{self.price})"



class PaymentTransaction(models.Model):
    TYPE_PAYMENT = "PAYMENT"
    TYPE_REFUND = "REFUND"
    TRANSACTION_TYPE_CHOICES = [
        (TYPE_PAYMENT, "Payment"),
        (TYPE_REFUND, "Refund"),
    ]

    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        related_name="payment_transactions",
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="payment_transactions",
    )
    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="transactions",
    )
    transaction_type = models.CharField(
        max_length=20,
        choices=TRANSACTION_TYPE_CHOICES,
        default=TYPE_PAYMENT,
    )
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
    )
    payment_method = models.CharField(
        max_length=30,
        choices=Order.PAYMENT_METHOD_CHOICES,
        default="CASH",
    )
    reference = models.CharField(
        max_length=100,
        blank=True,
        default="",
    )
    transaction_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recorded_payment_transactions",
    )
    note = models.TextField(
        blank=True,
        default="",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    def save(self, *args, **kwargs):
        if not self.branch_id:
            if self.order_id and self.order and self.order.branch_id:
                self.branch_id = self.order.branch_id
            elif self.restaurant_id:
                main_b = self.restaurant.branches.filter(is_main=True).first() or self.restaurant.branches.first()
                if main_b:
                    self.branch = main_b
        super().save(*args, **kwargs)

    class Meta:
        ordering = ["-transaction_at", "-id"]

    def __str__(self):
        return f"{self.transaction_type} #{self.id} - Order #{self.order_id} - {self.amount}"
