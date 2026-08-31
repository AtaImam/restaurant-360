from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from inventory.models import (
    Ingredient,
    IngredientCategory,
    IngredientPriceHistory,
    Recipe,
    RecipeIngredient,
    StorageLocation,
)
from menu.models import Category, MenuItem
from restaurant.models import Restaurant


class Command(BaseCommand):
    help = "Create Restaurant 360 demo data"

    def handle(self, *args, **options):

        self.stdout.write(
            self.style.WARNING(
                "Creating Restaurant 360 demo data..."
            )
        )

        # =====================================================
        # RESTAURANT
        # =====================================================

        restaurant, created = Restaurant.objects.get_or_create(
            name="FoodHub",
            defaults={
                "address": "Dhanmondi, Dhaka",
                "phone": "01700000000",
            },
        )

        if not created:
            restaurant.address = "Dhanmondi, Dhaka"
            restaurant.phone = "01700000000"
            restaurant.save()

        self.stdout.write(
            self.style.SUCCESS(
                "✓ Restaurant: FoodHub"
            )
        )

        # =====================================================
        # MENU CATEGORIES
        # =====================================================

        burger_category, _ = Category.objects.get_or_create(
            restaurant=restaurant,
            name="Burger",
        )

        pizza_category, _ = Category.objects.get_or_create(
            restaurant=restaurant,
            name="Pizza",
        )

        drinks_category, _ = Category.objects.get_or_create(
            restaurant=restaurant,
            name="Drinks",
        )

        self.stdout.write(
            self.style.SUCCESS(
                "✓ Menu categories created"
            )
        )

        # =====================================================
        # MENU ITEMS
        # =====================================================

        chicken_burger, _ = MenuItem.objects.update_or_create(
            category=burger_category,
            name="Chicken Burger",
            defaults={
                "description": (
                    "Crispy chicken burger with cheese, "
                    "lettuce and signature sauce."
                ),
                "price": Decimal("230.00"),
                "is_available": True,
            },
        )

        beef_burger, _ = MenuItem.objects.update_or_create(
            category=burger_category,
            name="Beef Burger",
            defaults={
                "description": (
                    "Juicy beef burger with cheese "
                    "and fresh vegetables."
                ),
                "price": Decimal("250.00"),
                "is_available": True,
            },
        )

        bbq_pizza, _ = MenuItem.objects.update_or_create(
            category=pizza_category,
            name="BBQ Pizza",
            defaults={
                "description": (
                    "BBQ chicken pizza with mozzarella "
                    "and smoky BBQ sauce."
                ),
                "price": Decimal("450.00"),
                "is_available": True,
            },
        )

        mojo, _ = MenuItem.objects.update_or_create(
            category=drinks_category,
            name="Mojo",
            defaults={
                "description": "Chilled soft drink.",
                "price": Decimal("50.00"),
                "is_available": True,
            },
        )

        self.stdout.write(
            self.style.SUCCESS(
                "✓ Menu items created"
            )
        )

        # =====================================================
        # INGREDIENT GROUPS
        # =====================================================

        proteins, _ = IngredientCategory.objects.get_or_create(
            restaurant=restaurant,
            name="Proteins",
        )

        dairy, _ = IngredientCategory.objects.get_or_create(
            restaurant=restaurant,
            name="Dairy",
        )

        vegetables, _ = IngredientCategory.objects.get_or_create(
            restaurant=restaurant,
            name="Vegetables",
        )

        bakery, _ = IngredientCategory.objects.get_or_create(
            restaurant=restaurant,
            name="Bakery",
        )

        dry_goods, _ = IngredientCategory.objects.get_or_create(
            restaurant=restaurant,
            name="Dry Goods",
        )

        sauces, _ = IngredientCategory.objects.get_or_create(
            restaurant=restaurant,
            name="Sauces",
        )

        beverages, _ = IngredientCategory.objects.get_or_create(
            restaurant=restaurant,
            name="Beverages",
        )

        self.stdout.write(
            self.style.SUCCESS(
                "✓ Ingredient groups created"
            )
        )

        # =====================================================
        # STORAGE LOCATIONS
        # =====================================================

        freezer, _ = StorageLocation.objects.get_or_create(
            restaurant=restaurant,
            name="Freezer",
        )

        refrigerator, _ = StorageLocation.objects.get_or_create(
            restaurant=restaurant,
            name="Refrigerator",
        )

        dry_store, _ = StorageLocation.objects.get_or_create(
            restaurant=restaurant,
            name="Dry Store",
        )

        beverage_store, _ = StorageLocation.objects.get_or_create(
            restaurant=restaurant,
            name="Beverage Store",
        )

        # =====================================================
        # HELPER
        # =====================================================

        today = timezone.localdate()

        def create_ingredient(
            name,
            sku,
            category,
            location,
            unit,
            pack_size,
            pack_price,
            stock,
            minimum,
            target,
            description="",
        ):

            ingredient, _ = Ingredient.objects.update_or_create(
                restaurant=restaurant,
                sku=sku,
                defaults={
                    "category": category,
                    "storage_location": location,
                    "name": name,
                    "description": description,
                    "base_unit": unit,
                    "pack_size": Decimal(str(pack_size)),
                    "current_pack_price": Decimal(
                        str(pack_price)
                    ),
                    "current_stock": Decimal(
                        str(stock)
                    ),
                    "minimum_level": Decimal(
                        str(minimum)
                    ),
                    "target_level": Decimal(
                        str(target)
                    ),
                    "is_active": True,
                },
            )

            IngredientPriceHistory.objects.update_or_create(
                ingredient=ingredient,
                effective_date=today,
                defaults={
                    "pack_price": Decimal(
                        str(pack_price)
                    ),
                },
            )

            return ingredient

        # =====================================================
        # INVENTORY ITEMS
        # =====================================================

        chicken_breast = create_ingredient(
            name="Chicken Breast",
            sku="CHK-001",
            category=proteins,
            location=freezer,
            unit=Ingredient.BaseUnit.GRAM,
            pack_size="5000",
            pack_price="2000",
            stock="5000",
            minimum="1000",
            target="8000",
            description="Boneless chicken breast.",
        )

        beef_patty = create_ingredient(
            name="Beef Patty",
            sku="BEF-001",
            category=proteins,
            location=freezer,
            unit=Ingredient.BaseUnit.GRAM,
            pack_size="5000",
            pack_price="3000",
            stock="4000",
            minimum="1000",
            target="7000",
            description="Prepared beef burger patty.",
        )

        burger_bun = create_ingredient(
            name="Burger Bun",
            sku="BUN-001",
            category=bakery,
            location=dry_store,
            unit=Ingredient.BaseUnit.PIECE,
            pack_size="20",
            pack_price="400",
            stock="60",
            minimum="15",
            target="100",
        )

        cheese = create_ingredient(
            name="Cheese Slice",
            sku="CHS-001",
            category=dairy,
            location=refrigerator,
            unit=Ingredient.BaseUnit.PIECE,
            pack_size="50",
            pack_price="750",
            stock="80",
            minimum="20",
            target="120",
        )

        lettuce = create_ingredient(
            name="Lettuce",
            sku="VEG-001",
            category=vegetables,
            location=refrigerator,
            unit=Ingredient.BaseUnit.GRAM,
            pack_size="1000",
            pack_price="250",
            stock="1500",
            minimum="300",
            target="2500",
        )

        tomato = create_ingredient(
            name="Tomato",
            sku="VEG-002",
            category=vegetables,
            location=refrigerator,
            unit=Ingredient.BaseUnit.GRAM,
            pack_size="1000",
            pack_price="180",
            stock="1800",
            minimum="400",
            target="3000",
        )

        cooking_oil = create_ingredient(
            name="Cooking Oil",
            sku="OIL-001",
            category=dry_goods,
            location=dry_store,
            unit=Ingredient.BaseUnit.MILLILITRE,
            pack_size="5000",
            pack_price="850",
            stock="7000",
            minimum="1500",
            target="10000",
        )

        pizza_dough = create_ingredient(
            name="Pizza Dough",
            sku="PIZ-001",
            category=bakery,
            location=refrigerator,
            unit=Ingredient.BaseUnit.GRAM,
            pack_size="5000",
            pack_price="700",
            stock="5000",
            minimum="1000",
            target="8000",
        )

        mozzarella = create_ingredient(
            name="Mozzarella Cheese",
            sku="CHS-002",
            category=dairy,
            location=refrigerator,
            unit=Ingredient.BaseUnit.GRAM,
            pack_size="2000",
            pack_price="1600",
            stock="2500",
            minimum="500",
            target="4000",
        )

        bbq_sauce = create_ingredient(
            name="BBQ Sauce",
            sku="SCE-001",
            category=sauces,
            location=refrigerator,
            unit=Ingredient.BaseUnit.MILLILITRE,
            pack_size="1000",
            pack_price="350",
            stock="1800",
            minimum="400",
            target="3000",
        )

        burger_sauce = create_ingredient(
            name="Burger Sauce",
            sku="SCE-002",
            category=sauces,
            location=refrigerator,
            unit=Ingredient.BaseUnit.MILLILITRE,
            pack_size="1000",
            pack_price="300",
            stock="1600",
            minimum="400",
            target="2500",
        )

        mojo_bottle = create_ingredient(
            name="Mojo 250ml",
            sku="DRK-001",
            category=beverages,
            location=beverage_store,
            unit=Ingredient.BaseUnit.PIECE,
            pack_size="24",
            pack_price="720",
            stock="72",
            minimum="24",
            target="120",
        )

        self.stdout.write(
            self.style.SUCCESS(
                "✓ Inventory items created"
            )
        )

        # =====================================================
        # RECIPE HELPER
        # =====================================================

        def create_recipe(
            menu_item,
            ingredients,
            instructions,
        ):

            recipe, _ = Recipe.objects.get_or_create(
                menu_item=menu_item,
                defaults={
                    "yield_quantity": Decimal("1"),
                },
            )

            recipe.yield_quantity = Decimal("1")
            recipe.instructions = instructions
            recipe.save()

            recipe.recipe_ingredients.all().delete()

            for ingredient, quantity in ingredients:

                RecipeIngredient.objects.create(
                    recipe=recipe,
                    ingredient=ingredient,
                    quantity=Decimal(
                        str(quantity)
                    ),
                )

        # =====================================================
        # CHICKEN BURGER RECIPE
        # =====================================================

        create_recipe(
            menu_item=chicken_burger,
            ingredients=[
                (
                    chicken_breast,
                    "120",
                ),
                (
                    burger_bun,
                    "1",
                ),
                (
                    cheese,
                    "1",
                ),
                (
                    lettuce,
                    "15",
                ),
                (
                    tomato,
                    "20",
                ),
                (
                    burger_sauce,
                    "15",
                ),
                (
                    cooking_oil,
                    "10",
                ),
            ],
            instructions=(
                "Cook chicken breast, toast the bun, "
                "add cheese, lettuce, tomato and burger sauce."
            ),
        )

        # =====================================================
        # BEEF BURGER RECIPE
        # =====================================================

        create_recipe(
            menu_item=beef_burger,
            ingredients=[
                (
                    beef_patty,
                    "130",
                ),
                (
                    burger_bun,
                    "1",
                ),
                (
                    cheese,
                    "1",
                ),
                (
                    lettuce,
                    "15",
                ),
                (
                    tomato,
                    "20",
                ),
                (
                    burger_sauce,
                    "15",
                ),
            ],
            instructions=(
                "Cook beef patty, toast bun and "
                "assemble with cheese and vegetables."
            ),
        )

        # =====================================================
        # BBQ PIZZA RECIPE
        # =====================================================

        create_recipe(
            menu_item=bbq_pizza,
            ingredients=[
                (
                    pizza_dough,
                    "250",
                ),
                (
                    chicken_breast,
                    "100",
                ),
                (
                    mozzarella,
                    "120",
                ),
                (
                    bbq_sauce,
                    "50",
                ),
            ],
            instructions=(
                "Prepare pizza base, add BBQ sauce, "
                "chicken and mozzarella, then bake."
            ),
        )

        # =====================================================
        # MOJO RECIPE
        # =====================================================

        create_recipe(
            menu_item=mojo,
            ingredients=[
                (
                    mojo_bottle,
                    "1",
                ),
            ],
            instructions=(
                "Serve one chilled bottle."
            ),
        )

        self.stdout.write(
            self.style.SUCCESS(
                "✓ Recipes created"
            )
        )

        # =====================================================
        # FINISH
        # =====================================================

        self.stdout.write("")

        self.stdout.write(
            self.style.SUCCESS(
                "=========================================="
            )
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Restaurant 360 demo database is ready!"
            )
        )

        self.stdout.write(
            self.style.SUCCESS(
                "=========================================="
            )
        )

        self.stdout.write("")

        self.stdout.write(
            "Demo Restaurant: FoodHub"
        )

        self.stdout.write(
            "Menu Items: 4"
        )

        self.stdout.write(
            "Inventory Items: 12"
        )

        self.stdout.write(
            "Recipes: 4"
        )