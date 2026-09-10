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