from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

from restaurant.models import Restaurant


class Category(models.Model):
    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        related_name="categories",
    )

    name = models.CharField(
        max_length=100
    )

    def __str__(self):
        return self.name


class MenuItem(models.Model):

    class ItemType(models.TextChoices):
        NORMAL = "NORMAL", "Normal Food"
        SET_MENU = "SET_MENU", "Set Menu"

    category = models.ForeignKey(
        Category,
        on_delete=models.CASCADE,
        related_name="items",
    )

    name = models.CharField(
        max_length=150
    )

    description = models.TextField(
        blank=True
    )

    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
    )

    image = models.ImageField(
        upload_to="menu/",
        blank=True,
        null=True,
    )

    item_type = models.CharField(
        max_length=20,
        choices=ItemType.choices,
        default=ItemType.NORMAL,
    )

    serves = models.PositiveSmallIntegerField(
        default=1,
        help_text="Number of people this item normally serves.",
    )

    is_available = models.BooleanField(
        default=True
    )

    @property
    def restaurant(self):
        return self.category.restaurant

    @property
    def is_set_menu(self):
        return self.item_type == self.ItemType.SET_MENU

    def get_effective_price(self, branch=None):
        if branch:
            override = getattr(self, "_branch_override", None)
            if override is None:
                override = self.branch_overrides.filter(branch=branch).first()
            if override and override.custom_price is not None:
                return override.custom_price
        return self.price

    def get_effective_availability(self, branch=None):
        if not self.is_available:
            return False
        if branch:
            override = getattr(self, "_branch_override", None)
            if override is None:
                override = self.branch_overrides.filter(branch=branch).first()
            if override is not None:
                return override.is_available
        return True

    def __str__(self):
        return self.name


class SetMenuComponent(models.Model):
    """
    Defines which normal menu items are included
    inside a Set Menu.

    Example:

    Set Menu 4
        Chicken Fried Rice x 1
        Fried Chicken x 1
        Chinese Vegetable x 1
        Chicken Chili Onion x 1
        Soft Drink x 1
    """

    set_menu = models.ForeignKey(
        MenuItem,
        on_delete=models.CASCADE,
        related_name="set_components",
    )

    component = models.ForeignKey(
        MenuItem,
        on_delete=models.PROTECT,
        related_name="included_in_sets",
    )

    quantity = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("1.00"),
        validators=[
            MinValueValidator(
                Decimal("0.01")
            )
        ],
        help_text=(
            "Quantity/portion of this food "
            "included in the set menu."
        ),
    )

    display_order = models.PositiveSmallIntegerField(
        default=0
    )

    class Meta:
        ordering = [
            "display_order",
            "id",
        ]

        constraints = [
            models.UniqueConstraint(
                fields=[
                    "set_menu",
                    "component",
                ],
                name=(
                    "unique_component_per_set_menu"
                ),
            )
        ]

    def clean(self):

        if (
            self.set_menu_id and
            self.component_id
        ):

            if (
                self.set_menu_id ==
                self.component_id
            ):
                raise ValidationError(
                    "A set menu cannot contain itself."
                )

            if (
                self.set_menu.item_type !=
                MenuItem.ItemType.SET_MENU
            ):
                raise ValidationError(
                    {
                        "set_menu":
                        "Selected item must be a Set Menu."
                    }
                )

            if (
                self.component.item_type !=
                MenuItem.ItemType.NORMAL
            ):
                raise ValidationError(
                    {
                        "component":
                        (
                            "A Set Menu can contain only "
                            "normal menu items."
                        )
                    }
                )

            set_restaurant_id = (
                self.set_menu.category.restaurant_id
            )

            component_restaurant_id = (
                self.component.category.restaurant_id
            )

            if (
                set_restaurant_id !=
                component_restaurant_id
            ):
                raise ValidationError(
                    (
                        "Set Menu and component must "
                        "belong to the same restaurant."
                    )
                )

    def save(self, *args, **kwargs):
        self.full_clean()

        return super().save(
            *args,
            **kwargs
        )

    def __str__(self):
        return (
            f"{self.set_menu.name} → "
            f"{self.component.name} "
            f"x {self.quantity}"
        )


class AddonGroup(models.Model):
    class SelectionType(models.TextChoices):
        SINGLE = "SINGLE", "Single Selection (Choose One)"
        MULTIPLE = "MULTIPLE", "Multiple Selection (Choose Many)"

    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        related_name="addon_groups",
    )
    name = models.CharField(max_length=120)
    selection_type = models.CharField(
        max_length=20,
        choices=SelectionType.choices,
        default=SelectionType.SINGLE,
    )
    is_required = models.BooleanField(
        default=False,
        help_text="Whether customer must make a selection from this group.",
    )
    min_selection = models.PositiveSmallIntegerField(
        default=0,
        help_text="Minimum options required to select.",
    )
    max_selection = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="Maximum options allowed to select (blank for unlimited in multiple).",
    )
    is_active = models.BooleanField(default=True)
    menu_items = models.ManyToManyField(
        MenuItem,
        related_name="addon_groups",
        blank=True,
    )
    display_order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_order", "id"]

    def clean(self):
        super().clean()
        if self.selection_type == self.SelectionType.SINGLE:
            self.max_selection = 1
            if self.is_required:
                self.min_selection = 1
            else:
                self.min_selection = 0
        else:
            if self.is_required and self.min_selection < 1:
                self.min_selection = 1
            if self.max_selection is not None:
                if self.max_selection < 1:
                    raise ValidationError({"max_selection": "Maximum selection must be at least 1."})
                if self.min_selection > self.max_selection:
                    raise ValidationError({"min_selection": "Minimum selection cannot exceed maximum selection."})

    def __str__(self):
        req_str = " (Required)" if self.is_required else ""
        return f"{self.name}{req_str}"


class AddonOption(models.Model):
    group = models.ForeignKey(
        AddonGroup,
        on_delete=models.CASCADE,
        related_name="options",
    )
    name = models.CharField(max_length=120)
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Extra price for this option (0 for free).",
    )
    is_active = models.BooleanField(default=True)
    display_order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_order", "id"]

    @property
    def restaurant(self):
        return self.group.restaurant

    def __str__(self):
        if self.price > Decimal("0.00"):
            return f"{self.name} (+৳{self.price})"
        return self.name


class BranchMenuItemOverride(models.Model):
    branch = models.ForeignKey(
        "restaurant.Branch",
        on_delete=models.CASCADE,
        related_name="menu_item_overrides",
    )
    menu_item = models.ForeignKey(
        MenuItem,
        on_delete=models.CASCADE,
        related_name="branch_overrides",
    )
    is_available = models.BooleanField(
        default=True,
        help_text="Branch-specific item availability."
    )
    custom_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Optional branch-specific price override."
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["branch", "menu_item"],
                name="unique_branch_menu_item_override",
            )
        ]

    def __str__(self):
        status = "Available" if self.is_available else "Unavailable"
        price_str = f" @ ৳{self.custom_price}" if self.custom_price is not None else ""
        return f"{self.branch.name} - {self.menu_item.name} ({status}{price_str})"