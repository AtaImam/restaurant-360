from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Sum
from django.utils import timezone

from menu.models import MenuItem
from orders.models import Order
from restaurant.models import Branch, Restaurant


class IngredientCategory(models.Model):
    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        related_name="ingredient_categories"
    )

    name = models.CharField(
        max_length=100
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["restaurant", "name"],
                name="unique_ingredient_category_per_restaurant"
            )
        ]

    def __str__(self):
        return self.name


class StorageLocation(models.Model):
    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        related_name="storage_locations"
    )

    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="storage_locations"
    )

    name = models.CharField(
        max_length=100
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["restaurant", "name"],
                name="unique_storage_location_per_restaurant"
            )
        ]

    def save(self, *args, **kwargs):
        if not self.branch_id and self.restaurant_id:
            main_b = self.restaurant.branches.filter(is_main=True).first() or self.restaurant.branches.first()
            if main_b:
                self.branch = main_b
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Allergen(models.Model):
    name = models.CharField(
        max_length=100,
        unique=True
    )

    def __str__(self):
        return self.name


class Ingredient(models.Model):
    class BaseUnit(models.TextChoices):
        GRAM = "G", "Gram (g)"
        MILLILITRE = "ML", "Millilitre (ml)"
        PIECE = "PCS", "Piece (pcs)"

    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        related_name="ingredients"
    )

    category = models.ForeignKey(
        IngredientCategory,
        on_delete=models.PROTECT,
        related_name="ingredients"
    )

    storage_location = models.ForeignKey(
        StorageLocation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ingredients"
    )

    name = models.CharField(
        max_length=150
    )

    sku = models.CharField(
        max_length=50
    )

    description = models.TextField(
        blank=True
    )

    base_unit = models.CharField(
        max_length=5,
        choices=BaseUnit.choices
    )

    pack_size = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))],
        help_text="Pack size converted into the base unit."
    )

    current_pack_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        default=0
    )

    current_stock = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.000"))],
        default=0
    )

    minimum_level = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.000"))],
        default=0
    )

    target_level = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.000"))],
        default=0
    )

    allergens = models.ManyToManyField(
        Allergen,
        blank=True,
        related_name="ingredients"
    )

    is_active = models.BooleanField(
        default=True
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    class Meta:
        ordering = ["name"]

        constraints = [
            models.UniqueConstraint(
                fields=["restaurant", "sku"],
                name="unique_ingredient_sku_per_restaurant"
            )
        ]

    @property
    def current_unit_cost(self):
        if self.pack_size <= 0:
            return Decimal("0.00")

        return self.current_pack_price / self.pack_size

    @property
    def reserved_stock(self):
        total = self.stock_reservations.filter(
            status=StockReservation.Status.ACTIVE
        ).aggregate(
            total=Sum("quantity")
        )["total"]

        return total or Decimal("0.000")

    @property
    def available_stock(self):
        available = self.current_stock - self.reserved_stock

        if available < 0:
            return Decimal("0.000")

        return available

    @property
    def stock_status(self):
        if self.available_stock <= 0:
            return "OUT"

        if self.available_stock <= self.minimum_level:
            return "LOW"

        return "OK"

    @property
    def reorder_quantity(self):
        shortage = self.target_level - self.available_stock

        if shortage > 0:
            return shortage

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new and self.restaurant_id:
            for branch in self.restaurant.branches.all():
                BranchIngredientStock.objects.get_or_create(
                    branch=branch,
                    ingredient=self,
                    defaults={
                        "current_stock": self.current_stock or Decimal("0.000"),
                        "reserved_stock": Decimal("0.000"),
                        "min_stock_alert": self.minimum_level or Decimal("0.000"),
                    },
                )

    def __str__(self):
        return self.name


class BranchIngredientStock(models.Model):
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="ingredient_stocks",
    )
    ingredient = models.ForeignKey(
        Ingredient,
        on_delete=models.CASCADE,
        related_name="branch_stocks",
    )
    current_stock = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.000"))],
        default=Decimal("0.000"),
    )
    reserved_stock = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.000"))],
        default=Decimal("0.000"),
    )
    min_stock_alert = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.000"))],
        default=Decimal("0.000"),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["branch", "ingredient"],
                name="unique_branch_ingredient_stock",
            )
        ]

    @property
    def available_stock(self):
        available = self.current_stock - self.reserved_stock
        return available if available > 0 else Decimal("0.000")

    @property
    def stock_status(self):
        if self.available_stock <= 0:
            return "OUT"
        if self.available_stock <= self.min_stock_alert:
            return "LOW"
        return "OK"

    def __str__(self):
        return f"{self.branch.name} - {self.ingredient.name}: {self.current_stock}"


class IngredientPriceHistory(models.Model):
    ingredient = models.ForeignKey(
        Ingredient,
        on_delete=models.CASCADE,
        related_name="price_history"
    )

    pack_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))]
    )

    effective_date = models.DateField()

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    class Meta:
        ordering = ["-effective_date", "-id"]

    def __str__(self):
        return (
            f"{self.ingredient.name} - "
            f"{self.pack_price}"
        )


class Recipe(models.Model):
    menu_item = models.OneToOneField(
        MenuItem,
        on_delete=models.CASCADE,
        related_name="recipe"
    )

    yield_quantity = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=1,
        validators=[MinValueValidator(Decimal("0.01"))]
    )

    instructions = models.TextField(
        blank=True
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    @property
    def estimated_food_cost(self):
        total = Decimal("0.00")

        for recipe_ingredient in self.recipe_ingredients.select_related(
            "ingredient"
        ):
            total += recipe_ingredient.estimated_cost

        yield_qty = self.yield_quantity if self.yield_quantity and self.yield_quantity > 0 else Decimal("1")
        return total / yield_qty

    def __str__(self):
        return f"Recipe - {self.menu_item.name}"

class RecipeIngredient(models.Model):
    recipe = models.ForeignKey(
        Recipe,
        on_delete=models.CASCADE,
        related_name="recipe_ingredients"
    )

    ingredient = models.ForeignKey(
        Ingredient,
        on_delete=models.PROTECT,
        related_name="recipe_usages"
    )

    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        validators=[
            MinValueValidator(
                Decimal("0.001")
            )
        ]
    )

    is_customer_visible = models.BooleanField(
        default=False,
        help_text=(
            "Show this ingredient name to customers "
            "on the menu item details page."
        )
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "recipe",
                    "ingredient"
                ],
                name="unique_ingredient_per_recipe"
            )
        ]

    @property
    def estimated_cost(self):
        return (
            self.quantity
            * self.ingredient.current_unit_cost
        )

    def __str__(self):
        return (
            f"{self.recipe.menu_item.name} - "
            f"{self.ingredient.name}"
        )


class AddonOptionIngredient(models.Model):
    addon_option = models.ForeignKey(
        "menu.AddonOption",
        on_delete=models.CASCADE,
        related_name="ingredient_requirements",
    )

    ingredient = models.ForeignKey(
        Ingredient,
        on_delete=models.PROTECT,
        related_name="addon_usages",
    )

    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        validators=[
            MinValueValidator(
                Decimal("0.001")
            )
        ],
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "addon_option",
                    "ingredient",
                ],
                name="unique_ingredient_per_addon_option",
            )
        ]

    def clean(self):
        super().clean()
        if self.addon_option_id and self.ingredient_id:
            addon_restaurant_id = self.addon_option.group.restaurant_id
            ingredient_restaurant_id = self.ingredient.restaurant_id
            if addon_restaurant_id != ingredient_restaurant_id:
                raise ValidationError("Addon option and ingredient must belong to the same restaurant.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    @property
    def estimated_cost(self):
        return (
            self.quantity
            * self.ingredient.current_unit_cost
        )

    def __str__(self):
        return (
            f"{self.addon_option.name} - "
            f"{self.ingredient.name} x {self.quantity}"
        )


class StockTransaction(models.Model):
    class TransactionType(models.TextChoices):
        OPENING_BALANCE = "OPENING_BALANCE", "Opening Balance"
        PURCHASE = "PURCHASE", "Purchase"
        CONSUMPTION = "CONSUMPTION", "Order Consumption"
        WASTE = "WASTE", "Waste"
        ADJUSTMENT_IN = "ADJUSTMENT_IN", "Adjustment In"
        ADJUSTMENT_OUT = "ADJUSTMENT_OUT", "Adjustment Out"
        RETURN = "RETURN", "Return"

    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="stock_transactions",
    )

    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="stock_transactions",
    )

    ingredient = models.ForeignKey(
        Ingredient,
        on_delete=models.PROTECT,
        related_name="stock_transactions"
    )

    transaction_type = models.CharField(
        max_length=30,
        choices=TransactionType.choices
    )

    quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))]
    )

    order = models.ForeignKey(
        Order,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_transactions"
    )

    unit_cost_snapshot = models.DecimalField(
        max_digits=12,
        decimal_places=6,
        validators=[MinValueValidator(Decimal("0.000000"))],
        default=Decimal("0.000000"),
    )

    note = models.TextField(
        blank=True
    )

    created_at = models.DateTimeField(
        default=timezone.now
    )

    def save(self, *args, **kwargs):
        if not self.restaurant_id and self.ingredient_id:
            self.restaurant = self.ingredient.restaurant
        if not self.branch_id and self.order_id and self.order.branch_id:
            self.branch = self.order.branch
        elif not self.branch_id and self.restaurant_id:
            main_b = self.restaurant.branches.filter(is_main=True).first() or self.restaurant.branches.first()
            if main_b:
                self.branch = main_b
        super().save(*args, **kwargs)

    @property
    def total_cost(self):
        return self.quantity * self.unit_cost_snapshot

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return (
            f"{self.ingredient.name} - "
            f"{self.get_transaction_type_display()}"
        )


class StockReservation(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        CONSUMED = "CONSUMED", "Consumed"
        RELEASED = "RELEASED", "Released"

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="stock_reservations"
    )

    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="stock_reservations",
    )

    ingredient = models.ForeignKey(
        Ingredient,
        on_delete=models.PROTECT,
        related_name="stock_reservations"
    )

    quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))]
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["order", "ingredient"],
                name="unique_order_ingredient_reservation"
            )
        ]

    def save(self, *args, **kwargs):
        if not self.branch_id and self.order_id and self.order.branch_id:
            self.branch = self.order.branch
        super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"Order #{self.order_id} - "
            f"{self.ingredient.name}"
        )


class WasteRecord(models.Model):
    class Reason(models.TextChoices):
        SPOILAGE = "SPOILAGE", "Spoilage"
        OVER_PREPARATION = "OVER_PREPARATION", "Over Preparation"
        KITCHEN_ERROR = "KITCHEN_ERROR", "Kitchen Error"
        EXPIRED = "EXPIRED", "Expired"
        DAMAGED = "DAMAGED", "Damaged"
        OTHER = "OTHER", "Other"

    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="waste_records",
    )

    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="waste_records",
    )

    ingredient = models.ForeignKey(
        Ingredient,
        on_delete=models.PROTECT,
        related_name="waste_records"
    )

    quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))]
    )

    reason = models.CharField(
        max_length=30,
        choices=Reason.choices
    )

    unit_cost_snapshot = models.DecimalField(
        max_digits=12,
        decimal_places=6,
        validators=[MinValueValidator(Decimal("0.000000"))],
        default=0
    )

    order = models.ForeignKey(
        Order,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="waste_records"
    )

    note = models.TextField(
        blank=True
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    def save(self, *args, **kwargs):
        if not self.restaurant_id and self.ingredient_id:
            self.restaurant = self.ingredient.restaurant
        if not self.branch_id and self.order_id and self.order.branch_id:
            self.branch = self.order.branch
        elif not self.branch_id and self.restaurant_id:
            main_b = self.restaurant.branches.filter(is_main=True).first() or self.restaurant.branches.first()
            if main_b:
                self.branch = main_b
        super().save(*args, **kwargs)

    @property
    def total_cost(self):
        return (
            self.quantity
            * self.unit_cost_snapshot
        )

    def __str__(self):
        return (
            f"{self.ingredient.name} - "
            f"{self.quantity}"
        )


class StockCount(models.Model):
    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="stock_counts",
    )

    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="stock_counts",
    )

    ingredient = models.ForeignKey(
        Ingredient,
        on_delete=models.PROTECT,
        related_name="stock_counts"
    )

    system_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3
    )

    actual_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.000"))]
    )

    note = models.TextField(
        blank=True
    )

    counted_at = models.DateTimeField(
        auto_now_add=True
    )

    def save(self, *args, **kwargs):
        if not self.restaurant_id and self.ingredient_id:
            self.restaurant = self.ingredient.restaurant
        if not self.branch_id and self.restaurant_id:
            main_b = self.restaurant.branches.filter(is_main=True).first() or self.restaurant.branches.first()
            if main_b:
                self.branch = main_b
        super().save(*args, **kwargs)

    @property
    def variance(self):
        return (
            self.actual_quantity
            - self.system_quantity
        )

    def __str__(self):
        return (
            f"{self.ingredient.name} "
            f"Stock Count"
        )


class Supplier(models.Model):
    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        related_name="suppliers",
    )
    name = models.CharField(max_length=150)
    contact_name = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["restaurant", "name"],
                name="unique_supplier_per_restaurant",
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.restaurant.name})"


class PurchaseOrder(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        RECEIVED = "RECEIVED", "Received"

    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        related_name="purchase_orders",
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="purchase_orders",
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="purchase_orders",
    )
    invoice_number = models.CharField(max_length=100, blank=True)
    purchase_date = models.DateField()
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    total_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    note = models.TextField(blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="received_purchase_orders",
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
        ordering = ["-purchase_date", "-id"]

    def __str__(self):
        return f"PO #{self.id} - {self.invoice_number or 'No Invoice'}"


class PurchaseOrderItem(models.Model):
    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.CASCADE,
        related_name="items",
    )
    ingredient = models.ForeignKey(
        Ingredient,
        on_delete=models.PROTECT,
        related_name="purchase_order_items",
    )
    pack_quantity = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    pack_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    total_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["purchase_order", "ingredient"],
                name="unique_ingredient_per_purchase_order",
            )
        ]

    def save(self, *args, **kwargs):
        self.total_price = self.pack_quantity * self.pack_price
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.purchase_order_id} - {self.ingredient.name} x {self.pack_quantity}"


class IngredientRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        ORDERED = "ORDERED", "Ordered"
        RECEIVED = "RECEIVED", "Received"

    class Priority(models.TextChoices):
        LOW = "LOW", "Low"
        MEDIUM = "MEDIUM", "Medium"
        HIGH = "HIGH", "High"
        URGENT = "URGENT", "Urgent"

    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        related_name="ingredient_requests",
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="ingredient_requests",
    )
    ingredient = models.ForeignKey(
        Ingredient,
        on_delete=models.CASCADE,
        related_name="requests",
    )
    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    priority = models.CharField(
        max_length=20,
        choices=Priority.choices,
        default=Priority.MEDIUM,
    )
    needed_date = models.DateField(null=True, blank=True)
    reason = models.TextField(blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ingredient_requests",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_ingredient_requests",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)

    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="converted_requests",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.branch_id and self.restaurant_id:
            main_b = self.restaurant.branches.filter(is_main=True).first() or self.restaurant.branches.first()
            if main_b:
                self.branch = main_b
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Request #{self.id} - {self.ingredient.name} ({self.quantity} {self.ingredient.unit}) - {self.status}"