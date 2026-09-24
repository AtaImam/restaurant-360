from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from restaurant.models import Branch, Restaurant


class ExpenseCategory(models.Model):
    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        related_name="expense_categories",
    )
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["restaurant", "name"],
                name="unique_expense_category_per_restaurant",
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.restaurant.name})"


class Expense(models.Model):
    PAYMENT_METHOD_CHOICES = [
        ("CASH", "Cash"),
        ("CARD", "Card"),
        ("BANK_TRANSFER", "Bank Transfer"),
        ("MOBILE_BANKING", "Mobile Banking"),
        ("OTHER", "Other"),
    ]

    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        related_name="expenses",
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="expenses",
    )
    category = models.ForeignKey(
        ExpenseCategory,
        on_delete=models.PROTECT,
        related_name="expenses",
    )
    title = models.CharField(max_length=150)
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    expense_date = models.DateField(default=timezone.localdate)
    payment_method = models.CharField(
        max_length=30,
        choices=PAYMENT_METHOD_CHOICES,
        default="CASH",
    )
    payee = models.CharField(
        max_length=150,
        blank=True,
        default="",
        help_text="Vendor or payee name",
    )
    receipt_image = models.ImageField(
        upload_to="expenses/%Y/%m/",
        blank=True,
        null=True,
    )
    notes = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_expenses",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if not self.branch_id and self.restaurant_id:
            main_b = self.restaurant.branches.filter(is_main=True).first() or self.restaurant.branches.first()
            if main_b:
                self.branch = main_b
        super().save(*args, **kwargs)

    class Meta:
        ordering = ["-expense_date", "-id"]

    def __str__(self):
        return f"{self.title} - {self.amount} ({self.expense_date})"
