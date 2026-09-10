from django.db import models

from menu.models import MenuItem
from restaurant.models import Restaurant, Table


class Order(models.Model):

    ORDER_TYPES = [
        ("DINE_IN", "Dine In"),
        ("TAKEAWAY", "Takeaway"),
    ]

    STATUS_CHOICES = [
        ("NEW", "New"),
        ("ACCEPTED", "Accepted"),
        ("PREPARING", "Preparing"),
        ("READY", "Ready"),
        ("SERVED", "Served"),
        ("COMPLETED", "Completed"),
    ]

    PAYMENT_STATUS_CHOICES = [
        ("UNPAID", "Unpaid"),
        ("PAID", "Paid"),
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

    table = models.ForeignKey(
        Table,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
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

    created_at = models.DateTimeField(
        auto_now_add=True
    )

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

    @property
    def subtotal(self):
        return self.price * self.quantity

    def __str__(self):
        return f"{self.menu_item.name} x {self.quantity}"