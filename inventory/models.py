from django.db import models

# Create your models here.
from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Sum

from menu.models import MenuItem
from orders.models import Order
from restaurant.models import Restaurant


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

        return Decimal("0.000")

    def __str__(self):
        return self.name


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

        return total

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

class StockTransaction(models.Model):
    class TransactionType(models.TextChoices):
        PURCHASE = "PURCHASE", "Purchase"
        CONSUMPTION = "CONSUMPTION", "Order Consumption"
        WASTE = "WASTE", "Waste"
        ADJUSTMENT_IN = "ADJUSTMENT_IN", "Adjustment In"
        ADJUSTMENT_OUT = "ADJUSTMENT_OUT", "Adjustment Out"
        RETURN = "RETURN", "Return"

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

    note = models.TextField(
        blank=True
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

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