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
from menu.models import MenuItem
from restaurant.models import Restaurant


class Command(BaseCommand):
    help = (
        "Build realistic Chinese food recipes "
        "and connect them to inventory."
    )

    @transaction.atomic
    def handle(self, *args, **options):

        restaurant = Restaurant.objects.first()

        if not restaurant:
            raise CommandError(
                "No restaurant found."
            )

        # =====================================================
        # CATEGORY + STORAGE
        # =====================================================

        def get_category(name):
            obj, _ = (
                IngredientCategory.objects.get_or_create(
                    restaurant=restaurant,
                    name=name,
                )
            )

            return obj


        def get_location(name):
            obj, _ = (
                StorageLocation.objects.get_or_create(
                    restaurant=restaurant,
                    name=name,
                )
            )

            return obj


        dry_store = get_location(
            "Dry Store"
        )

        chiller = get_location(
            "Chiller"
        )

        freezer = get_location(
            "Freezer"
        )

        beverage_store = get_location(
            "Beverage Store"
        )


        grain_category = get_category(
            "Rice & Grains"
        )

        meat_category = get_category(
            "Meat & Poultry"
        )

        vegetable_category = get_category(
            "Vegetables"
        )

        sauce_category = get_category(
            "Sauces & Condiments"
        )

        dry_category = get_category(
            "Dry Ingredients"
        )

        beverage_category = get_category(
            "Beverages"
        )


        # =====================================================
        # SAFE INGREDIENT HELPER
        # =====================================================

        def ingredient(
            name,
            sku,
            category,
            location,
            unit,
            pack_size,
            pack_price,
            opening_stock,
            minimum_level,
            target_level,
        ):

            # -------------------------------------
            # 1. Try exact SKU first
            # -------------------------------------

            obj = Ingredient.objects.filter(
                restaurant=restaurant,
                sku=sku,
            ).first()


            if obj:

                self.stdout.write(
                    f"Reuse by SKU: "
                    f"{obj.name} [{obj.sku}]"
                )

                if not obj.is_active:
                    obj.is_active = True
                    obj.save(
                        update_fields=[
                            "is_active"
                        ]
                    )

                return obj


            # -------------------------------------
            # 2. Try ingredient name
            # -------------------------------------

            obj = Ingredient.objects.filter(
                restaurant=restaurant,
                name__iexact=name,
            ).first()


            if obj:

                self.stdout.write(
                    f"Reuse by name: "
                    f"{obj.name} [{obj.sku}]"
                )

                if not obj.is_active:
                    obj.is_active = True
                    obj.save(
                        update_fields=[
                            "is_active"
                        ]
                    )

                return obj


            # -------------------------------------
            # 3. Only create if genuinely new
            # -------------------------------------

            obj = Ingredient.objects.create(
                restaurant=restaurant,
                category=category,
                storage_location=location,
                name=name,
                sku=sku,
                base_unit=unit,
                pack_size=Decimal(
                    str(pack_size)
                ),
                current_pack_price=Decimal(
                    str(pack_price)
                ),
                current_stock=Decimal(
                    str(opening_stock)
                ),
                minimum_level=Decimal(
                    str(minimum_level)
                ),
                target_level=Decimal(
                    str(target_level)
                ),
                is_active=True,
            )


            self.stdout.write(
                f"Created ingredient: "
                f"{obj.name} [{obj.sku}]"
            )


            return obj


        # =====================================================
        # INGREDIENTS
        # =====================================================

        rice = ingredient(
            "Rice",
            "RICE-001",
            grain_category,
            dry_store,
            Ingredient.BaseUnit.GRAM,
            1000,
            120,
            10000,
            2000,
            10000,
        )


        chicken = ingredient(
            "Chicken",
            "CHK-001",
            meat_category,
            freezer,
            Ingredient.BaseUnit.GRAM,
            1000,
            280,
            10000,
            2000,
            10000,
        )


        beef = ingredient(
            "Beef",
            "BEF-001",
            meat_category,
            freezer,
            Ingredient.BaseUnit.GRAM,
            1000,
            800,
            5000,
            1000,
            5000,
        )


        egg = ingredient(
            "Egg",
            "EGG-001",
            meat_category,
            chiller,
            Ingredient.BaseUnit.PIECE,
            12,
            150,
            60,
            12,
            60,
        )


        carrot = ingredient(
            "Carrot",
            "VEG-CARROT",
            vegetable_category,
            chiller,
            Ingredient.BaseUnit.GRAM,
            1000,
            120,
            3000,
            500,
            3000,
        )


        beans = ingredient(
            "Beans",
            "VEG-BEANS",
            vegetable_category,
            chiller,
            Ingredient.BaseUnit.GRAM,
            1000,
            180,
            2000,
            400,
            2000,
        )


        onion = ingredient(
            "Onion",
            "VEG-ONION",
            vegetable_category,
            dry_store,
            Ingredient.BaseUnit.GRAM,
            1000,
            120,
            5000,
            1000,
            5000,
        )


        capsicum = ingredient(
            "Capsicum",
            "VEG-CAPSICUM",
            vegetable_category,
            chiller,
            Ingredient.BaseUnit.GRAM,
            1000,
            300,
            2000,
            400,
            2000,
        )


        cabbage = ingredient(
            "Cabbage",
            "VEG-CABBAGE",
            vegetable_category,
            chiller,
            Ingredient.BaseUnit.GRAM,
            1000,
            80,
            3000,
            500,
            3000,
        )


        green_chili = ingredient(
            "Green Chili",
            "VEG-CHILI",
            vegetable_category,
            chiller,
            Ingredient.BaseUnit.GRAM,
            1000,
            200,
            1000,
            200,
            1000,
        )


        garlic = ingredient(
            "Garlic",
            "VEG-GARLIC",
            vegetable_category,
            dry_store,
            Ingredient.BaseUnit.GRAM,
            1000,
            250,
            2000,
            300,
            2000,
        )


        cooking_oil = ingredient(
            "Cooking Oil",
            "OIL-001",
            sauce_category,
            dry_store,
            Ingredient.BaseUnit.MILLILITRE,
            1000,
            200,
            10000,
            2000,
            10000,
        )


        soy_sauce = ingredient(
            "Soy Sauce",
            "SAUCE-SOY",
            sauce_category,
            dry_store,
            Ingredient.BaseUnit.MILLILITRE,
            1000,
            250,
            3000,
            500,
            3000,
        )


        chili_sauce = ingredient(
            "Chili Sauce",
            "SAUCE-CHILI",
            sauce_category,
            dry_store,
            Ingredient.BaseUnit.MILLILITRE,
            1000,
            300,
            3000,
            500,
            3000,
        )


        ketchup = ingredient(
            "Tomato Ketchup",
            "SAUCE-KETCHUP",
            sauce_category,
            dry_store,
            Ingredient.BaseUnit.MILLILITRE,
            1000,
            250,
            3000,
            500,
            3000,
        )


        vinegar = ingredient(
            "Vinegar",
            "SAUCE-VINEGAR",
            sauce_category,
            dry_store,
            Ingredient.BaseUnit.MILLILITRE,
            1000,
            180,
            2000,
            300,
            2000,
        )


        flour = ingredient(
            "Flour",
            "DRY-FLOUR",
            dry_category,
            dry_store,
            Ingredient.BaseUnit.GRAM,
            1000,
            70,
            5000,
            1000,
            5000,
        )


        corn_flour = ingredient(
            "Corn Flour",
            "DRY-CORN",
            dry_category,
            dry_store,
            Ingredient.BaseUnit.GRAM,
            500,
            90,
            2000,
            400,
            2000,
        )


        salt = ingredient(
            "Salt",
            "DRY-SALT",
            dry_category,
            dry_store,
            Ingredient.BaseUnit.GRAM,
            1000,
            50,
            3000,
            500,
            3000,
        )


        black_pepper = ingredient(
            "Black Pepper",
            "DRY-PEPPER",
            dry_category,
            dry_store,
            Ingredient.BaseUnit.GRAM,
            100,
            180,
            500,
            100,
            500,
        )


        wonton_wrapper = ingredient(
            "Wonton Wrapper",
            "DRY-WONTON",
            dry_category,
            chiller,
            Ingredient.BaseUnit.PIECE,
            50,
            180,
            200,
            50,
            200,
        )


        soft_drink_stock = ingredient(
            "Soft Drink Bottle",
            "DRINK-SOFT",
            beverage_category,
            beverage_store,
            Ingredient.BaseUnit.PIECE,
            1,
            25,
            100,
            20,
            100,
        )


        # =====================================================
        # MENU ITEM HELPER
        # =====================================================

        def get_menu_item(name):

            item = MenuItem.objects.filter(
                category__restaurant=restaurant,
                name__iexact=name,
                is_available=True,
            ).first()


            if not item:

                raise CommandError(
                    f"Menu item not found: {name}"
                )


            return item


        # =====================================================
        # RECIPE HELPER
        # =====================================================

        def build_recipe(
            menu_name,
            ingredients,
            instructions,
        ):

            menu_item = get_menu_item(
                menu_name
            )


            recipe, _ = (
                Recipe.objects.update_or_create(
                    menu_item=menu_item,
                    defaults={
                        "yield_quantity":
                            Decimal("1.00"),

                        "instructions":
                            instructions,
                    },
                )
            )


            used_ids = []


            for (
                ingredient_obj,
                quantity
            ) in ingredients:

                RecipeIngredient.objects.update_or_create(
                    recipe=recipe,
                    ingredient=ingredient_obj,
                    defaults={
                        "quantity":
                            Decimal(
                                str(quantity)
                            )
                    },
                )


                used_ids.append(
                    ingredient_obj.id
                )


            RecipeIngredient.objects.filter(
                recipe=recipe
            ).exclude(
                ingredient_id__in=used_ids
            ).delete()


            self.stdout.write(
                self.style.SUCCESS(
                    f"Recipe ready: "
                    f"{menu_item.name}"
                )
            )


        # =====================================================
        # CHICKEN FRIED RICE
        # =====================================================

        build_recipe(
            "Chicken Fried Rice",
            [
                (rice, 250),
                (chicken, 80),
                (egg, 1),
                (carrot, 30),
                (beans, 20),
                (onion, 20),
                (soy_sauce, 15),
                (cooking_oil, 20),
                (salt, 3),
            ],
            (
                "Cook rice, stir-fry chicken, egg "
                "and vegetables, then combine with "
                "soy sauce and seasoning."
            ),
        )


        # =====================================================
        # EGG FRIED RICE
        # =====================================================

        build_recipe(
            "Egg Fried Rice",
            [
                (rice, 250),
                (egg, 2),
                (carrot, 30),
                (beans, 20),
                (onion, 20),
                (soy_sauce, 15),
                (cooking_oil, 20),
                (salt, 3),
            ],
            (
                "Stir-fry egg and vegetables, "
                "add cooked rice and seasoning."
            ),
        )


        # =====================================================
        # FRIED CHICKEN
        # =====================================================

        build_recipe(
            "Fried Chicken",
            [
                (chicken, 150),
                (flour, 30),
                (egg, Decimal("0.50")),
                (cooking_oil, 25),
                (salt, 2),
                (black_pepper, 1),
            ],
            (
                "Season chicken, coat with egg "
                "and flour, then fry until cooked."
            ),
        )


        # =====================================================
        # CHINESE VEGETABLE
        # =====================================================

        build_recipe(
            "Chinese Vegetable",
            [
                (cabbage, 80),
                (carrot, 40),
                (capsicum, 30),
                (beans, 30),
                (onion, 20),
                (soy_sauce, 10),
                (corn_flour, 8),
                (cooking_oil, 10),
                (salt, 2),
            ],
            (
                "Stir-fry vegetables and finish "
                "with light Chinese-style sauce."
            ),
        )


        # =====================================================
        # CHICKEN CHILI ONION
        # =====================================================

        build_recipe(
            "Chicken Chili Onion",
            [
                (chicken, 120),
                (onion, 60),
                (capsicum, 40),
                (green_chili, 10),
                (soy_sauce, 15),
                (chili_sauce, 15),
                (corn_flour, 10),
                (cooking_oil, 15),
            ],
            (
                "Cook chicken with onion, capsicum, "
                "green chili and savory sauces."
            ),
        )


        # =====================================================
        # BEEF CHILI ONION
        # =====================================================

        build_recipe(
            "Beef Chili Onion",
            [
                (beef, 120),
                (onion, 60),
                (capsicum, 40),
                (green_chili, 10),
                (soy_sauce, 15),
                (chili_sauce, 15),
                (corn_flour, 10),
                (cooking_oil, 15),
            ],
            (
                "Cook sliced beef with onion, "
                "capsicum and savory chili sauce."
            ),
        )


        # =====================================================
        # CHICKEN MANCHURIAN
        # =====================================================

        build_recipe(
            "Chicken Manchurian",
            [
                (chicken, 120),
                (corn_flour, 20),
                (garlic, 10),
                (chili_sauce, 20),
                (soy_sauce, 10),
                (ketchup, 20),
                (cooking_oil, 20),
            ],
            (
                "Cook coated chicken and toss "
                "with garlic and Manchurian sauce."
            ),
        )


        # =====================================================
        # THAI SOUP
        # =====================================================

        build_recipe(
            "Thai Soup",
            [
                (chicken, 60),
                (egg, Decimal("0.50")),
                (corn_flour, 10),
                (chili_sauce, 10),
                (ketchup, 15),
                (vinegar, 10),
                (soy_sauce, 10),
                (green_chili, 5),
            ],
            (
                "Prepare hot and tangy soup "
                "with chicken, egg and sauces."
            ),
        )


        # =====================================================
        # FRIED WONTON
        # =====================================================

        build_recipe(
            "Fried Wonton",
            [
                (wonton_wrapper, 6),
                (chicken, 80),
                (cabbage, 20),
                (onion, 15),
                (cooking_oil, 20),
            ],
            (
                "Fill wonton wrappers with "
                "seasoned chicken and vegetables "
                "then fry until crisp."
            ),
        )


        # =====================================================
        # SOFT DRINK
        # =====================================================

        build_recipe(
            "Soft Drink",
            [
                (soft_drink_stock, 1),
            ],
            (
                "Serve one chilled soft drink."
            ),
        )


        # =====================================================
        # RESULT
        # =====================================================

        ingredient_count = (
            Ingredient.objects.filter(
                restaurant=restaurant
            ).count()
        )


        recipe_count = (
            Recipe.objects.filter(
                menu_item__category__restaurant=
                    restaurant
            ).count()
        )


        self.stdout.write("")


        self.stdout.write(
            self.style.SUCCESS(
                "Recipes and inventory "
                "successfully connected."
            )
        )


        self.stdout.write(
            f"Restaurant: "
            f"{restaurant.name}"
        )


        self.stdout.write(
            f"Total ingredients: "
            f"{ingredient_count}"
        )


        self.stdout.write(
            f"Total recipes: "
            f"{recipe_count}"
        )