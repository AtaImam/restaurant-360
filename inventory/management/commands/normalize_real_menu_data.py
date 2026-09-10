from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from inventory.models import (
    Ingredient,
    IngredientCategory,
    Recipe,
    RecipeIngredient,
    StorageLocation,
)
from menu.models import Category, MenuItem
from restaurant.models import Restaurant


class Command(BaseCommand):
    help = (
        "Normalize Restaurant 360 menu and recipe data "
        "for realistic restaurant usage."
    )

    @transaction.atomic
    def handle(self, *args, **options):

        restaurant = Restaurant.objects.first()

        if not restaurant:
            raise CommandError(
                "No restaurant found."
            )

        self.stdout.write(
            f"Restaurant: {restaurant.name}"
        )

        # =====================================================
        # 1. FIX DUPLICATE BEEF BURGER
        # =====================================================

        beef_burgers = (
            MenuItem.objects.filter(
                category__restaurant=restaurant,
                name__iexact="Beef Burger",
                is_available=True,
            )
            .select_related("category")
            .order_by("id")
        )

        preferred_beef_burger = (
            beef_burgers.filter(
                category__name__iexact="Burger"
            ).first()
        )

        if not preferred_beef_burger:
            preferred_beef_burger = (
                beef_burgers.first()
            )

        archived_burgers = 0

        if preferred_beef_burger:

            for burger in beef_burgers:

                if (
                    burger.id !=
                    preferred_beef_burger.id
                ):
                    burger.is_available = False

                    burger.save(
                        update_fields=[
                            "is_available"
                        ]
                    )

                    archived_burgers += 1

                    self.stdout.write(
                        f"Archived duplicate: "
                        f"#{burger.id} "
                        f"{burger.name} "
                        f"({burger.category.name})"
                    )

        # =====================================================
        # 2. NORMALIZE CHICKEN KACCHI CATEGORY
        # =====================================================

        biryani_category = (
            Category.objects.filter(
                restaurant=restaurant,
                name__iexact="Biryani & Rice",
            ).first()
        )

        if not biryani_category:
            biryani_category = (
                Category.objects.create(
                    restaurant=restaurant,
                    name="Biryani & Rice",
                )
            )

        chicken_kacchis = (
            MenuItem.objects.filter(
                category__restaurant=restaurant,
                name__iexact="Chicken Kacchi",
                is_available=True,
            )
        )

        chicken_kacchi_moved = (
            chicken_kacchis.update(
                category=biryani_category
            )
        )

        # =====================================================
        # 3. NORMALIZE DRINK CATEGORY
        # =====================================================

        beverage_category = (
            Category.objects.filter(
                restaurant=restaurant,
                name__iexact="Beverages",
            ).first()
        )

        if not beverage_category:
            beverage_category = (
                Category.objects.create(
                    restaurant=restaurant,
                    name="Beverages",
                )
            )

        drinks_moved = (
            MenuItem.objects.filter(
                category__restaurant=restaurant,
                name__in=[
                    "Mojo",
                    "Soft Drink",
                ],
                is_available=True,
            )
            .update(
                category=beverage_category
            )
        )

        # =====================================================
        # 4. CREATE / REUSE RAW BEEF
        # =====================================================

        beef_patty = (
            Ingredient.objects.filter(
                restaurant=restaurant,
                name__iexact="Beef Patty",
            ).first()
        )

        raw_beef = (
            Ingredient.objects.filter(
                restaurant=restaurant,
                name__iexact="Beef",
            ).first()
        )

        if not raw_beef:

            raw_beef = (
                Ingredient.objects.filter(
                    restaurant=restaurant,
                    sku="RAW-BEEF-001",
                ).first()
            )

        if not raw_beef:

            if beef_patty:
                meat_category = (
                    beef_patty.category
                )

                storage_location = (
                    beef_patty.storage_location
                )

            else:
                meat_category, _ = (
                    IngredientCategory.objects.get_or_create(
                        restaurant=restaurant,
                        name="Meat & Poultry",
                    )
                )

                storage_location, _ = (
                    StorageLocation.objects.get_or_create(
                        restaurant=restaurant,
                        name="Freezer",
                    )
                )

            raw_beef = Ingredient.objects.create(
                restaurant=restaurant,
                category=meat_category,
                storage_location=storage_location,
                name="Beef",
                sku="RAW-BEEF-001",
                base_unit=Ingredient.BaseUnit.GRAM,
                pack_size=Decimal("1000"),
                current_pack_price=Decimal("800"),
                current_stock=Decimal("5000"),
                minimum_level=Decimal("1000"),
                target_level=Decimal("5000"),
                is_active=True,
            )

            self.stdout.write(
                "Created ingredient: Beef [RAW-BEEF-001]"
            )

        # =====================================================
        # 5. BEEF PATTY SHOULD ONLY BE USED FOR BURGER
        # =====================================================

        raw_beef_dishes = [
            "Beef Tehari",
            "Beef Bhuna",
            "Beef Chili Onion",
            "Beef Seekh Kebab",
        ]

        beef_recipe_fixes = 0

        if beef_patty:

            wrong_beef_rows = (
                RecipeIngredient.objects.filter(
                    ingredient=beef_patty,
                    recipe__menu_item__category__restaurant=
                        restaurant,
                    recipe__menu_item__name__in=
                        raw_beef_dishes,
                )
                .select_related(
                    "recipe",
                    "recipe__menu_item",
                )
            )

            for old_row in list(
                wrong_beef_rows
            ):

                RecipeIngredient.objects.update_or_create(
                    recipe=old_row.recipe,
                    ingredient=raw_beef,
                    defaults={
                        "quantity":
                            old_row.quantity,

                        "is_customer_visible":
                            True,
                    },
                )

                old_row.delete()

                beef_recipe_fixes += 1

                self.stdout.write(
                    f"Fixed raw beef recipe: "
                    f"{old_row.recipe.menu_item.name}"
                )

        # =====================================================
        # 6. CUSTOMER SHOULD SEE CHICKEN IN CHICKEN DISHES
        # =====================================================

        customer_chicken_dishes = [
            "Chicken Fried Rice",
            "Chicken Chili Onion",
            "Chicken Manchurian",
            "Fried Chicken",
            "Fried Wonton",
            "Thai Soup",
            "Chicken Chow Mein",
            "Chicken Curry",
            "Chicken Biryani",
            "Chicken Kacchi",
            "Chicken Shawarma",
            "Chicken Tikka",
            "Tandoori Chicken",
        ]

        chicken_visibility_fixed = (
            RecipeIngredient.objects.filter(
                recipe__menu_item__category__restaurant=
                    restaurant,
                recipe__menu_item__name__in=
                    customer_chicken_dishes,
                ingredient__name__iexact=
                    "Chicken Breast",
            )
            .update(
                is_customer_visible=True
            )
        )

        # =====================================================
        # 7. MOJO MUST CONSUME ACTUAL MOJO STOCK
        # =====================================================

        mojo_menu = (
            MenuItem.objects.filter(
                category__restaurant=restaurant,
                name__iexact="Mojo",
                is_available=True,
            ).first()
        )

        mojo_stock = (
            Ingredient.objects.filter(
                restaurant=restaurant,
                name__iexact="Mojo 250ml",
            ).first()
        )

        mojo_fixed = False

        if mojo_menu and mojo_stock:

            mojo_recipe = getattr(
                mojo_menu,
                "recipe",
                None,
            )

            if mojo_recipe:

                RecipeIngredient.objects.filter(
                    recipe=mojo_recipe
                ).delete()

                RecipeIngredient.objects.create(
                    recipe=mojo_recipe,
                    ingredient=mojo_stock,
                    quantity=Decimal("1.000"),
                    is_customer_visible=True,
                )

                mojo_fixed = True

        # =====================================================
        # 8. FINAL SEMANTIC AUDIT
        # =====================================================

        active_beef_burgers = (
            MenuItem.objects.filter(
                category__restaurant=restaurant,
                name__iexact="Beef Burger",
                is_available=True,
            )
        )

        wrong_beef_usage = (
            RecipeIngredient.objects.filter(
                recipe__menu_item__category__restaurant=
                    restaurant,
                recipe__menu_item__name__in=
                    raw_beef_dishes,
                ingredient__name__iexact=
                    "Beef Patty",
            )
        )

        hidden_chicken = (
            RecipeIngredient.objects.filter(
                recipe__menu_item__category__restaurant=
                    restaurant,
                recipe__menu_item__name__in=
                    customer_chicken_dishes,
                ingredient__name__iexact=
                    "Chicken Breast",
                is_customer_visible=False,
            )
        )

        mojo_wrong_usage = (
            RecipeIngredient.objects.filter(
                recipe__menu_item=mojo_menu,
            )
            .exclude(
                ingredient=mojo_stock
            )
            if mojo_menu and mojo_stock
            else RecipeIngredient.objects.none()
        )

        self.stdout.write("")
        self.stdout.write(
            "=" * 60
        )

        self.stdout.write(
            self.style.SUCCESS(
                "NORMALIZATION COMPLETE"
            )
        )

        self.stdout.write(
            "=" * 60
        )

        self.stdout.write(
            f"Duplicate Beef Burgers archived: "
            f"{archived_burgers}"
        )

        self.stdout.write(
            f"Chicken Kacchi moved to Biryani & Rice: "
            f"{chicken_kacchi_moved}"
        )

        self.stdout.write(
            f"Drinks moved to Beverages: "
            f"{drinks_moved}"
        )

        self.stdout.write(
            f"Raw-beef recipes corrected: "
            f"{beef_recipe_fixes}"
        )

        self.stdout.write(
            f"Chicken visibility rows updated: "
            f"{chicken_visibility_fixed}"
        )

        self.stdout.write(
            f"Mojo stock mapping fixed: "
            f"{'YES' if mojo_fixed else 'NO'}"
        )

        self.stdout.write("")
        self.stdout.write(
            "=== FINAL CHECK ==="
        )

        self.stdout.write(
            f"Active Beef Burger records: "
            f"{active_beef_burgers.count()}"
        )

        self.stdout.write(
            f"Non-burger dishes using Beef Patty: "
            f"{wrong_beef_usage.count()}"
        )

        self.stdout.write(
            f"Chicken dishes hiding chicken: "
            f"{hidden_chicken.count()}"
        )

        self.stdout.write(
            f"Mojo wrong ingredient mappings: "
            f"{mojo_wrong_usage.count()}"
        )

        if (
            active_beef_burgers.count() == 1
            and
            wrong_beef_usage.count() == 0
            and
            hidden_chicken.count() == 0
            and
            mojo_wrong_usage.count() == 0
        ):

            self.stdout.write("")
            self.stdout.write(
                self.style.SUCCESS(
                    "MENU DATA IS NOW SEMANTICALLY CLEAN."
                )
            )

        else:

            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    "Some semantic issues remain."
                )
            )