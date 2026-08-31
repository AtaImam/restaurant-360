from django.db import models
from restaurant.models import Restaurant, Table
from menu.models import MenuItem


class Order(models.Model):

    ORDER_TYPES = [
        ('DINE_IN', 'Dine In'),
        ('TAKEAWAY', 'Takeaway'),
    ]

    STATUS_CHOICES = [
        ('NEW', 'New'),
        ('ACCEPTED', 'Accepted'),
        ('PREPARING', 'Preparing'),
        ('READY', 'Ready'),
        ('SERVED', 'Served'),
        ('COMPLETED', 'Completed'),
    ]

    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        related_name='orders'
    )

    table = models.ForeignKey(
        Table,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    order_type = models.CharField(
        max_length=20,
        choices=ORDER_TYPES
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='NEW'
    )

    total_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Order #{self.id}"


class OrderItem(models.Model):
    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name='items'
    )

    menu_item = models.ForeignKey(
        MenuItem,
        on_delete=models.PROTECT
    )

    quantity = models.PositiveIntegerField()

    price = models.DecimalField(
        max_digits=10,
        decimal_places=2
    )
    @property
    def subtotal(self):
         return self.price * self.quantity 

    def __str__(self):
        return f"{self.menu_item.name} x {self.quantity}"