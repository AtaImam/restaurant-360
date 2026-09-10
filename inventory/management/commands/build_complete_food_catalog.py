from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from inventory.models import (
    Allergen,
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
        "Complete missing food descriptions, recipes and "
        "customer-visible ingredients for the demo restaurant."
    )

    @transaction.atomic
    def handle(self, *args, **options):

        restaurant = Restaurant.objects.first()

        if not restaurant:
            raise CommandError(
                "No restaurant found."
            )

        # =====================================================
        # SAFETY CHECKS
        # =====================================================

        recipe_ingredient_fields = {
            field.name
            for field in RecipeIngredient._meta.fields
        }

        if (
            "is_customer_visible"
            not in recipe_ingredient_fields
        ):
            raise CommandError(
                "RecipeIngredient.is_customer_visible is missing. "
                "Run the inventory migration first."
            )

        if not hasattr(
            MenuItem,
            "ItemType",
        ):
            raise CommandError(
                "MenuItem.item_type architecture is missing."
            )

        # =====================================================
        # CATEGORY / STORAGE HELPERS
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


        categories = {
            name: get_category(name)
            for name in [
                "Rice & Grains",
                "Meat & Poultry",
                "Seafood",
                "Vegetables",
                "Dairy",
                "Spices",
                "Sauces & Condiments",
                "Dry Ingredients",
                "Noodles & Pasta",
                "Bakery",
                "Dessert Ingredients",
                "Beverages",
            ]
        }


        locations = {
            name: get_location(name)
            for name in [
                "Dry Store",
                "Chiller",
                "Freezer",
                "Beverage Store",
            ]
        }

        # =====================================================
        # INGREDIENT CATALOG
        #
        # Existing ingredients are REUSED.
        # Existing stock/cost is NOT overwritten.
        #
        # New ingredients receive realistic demo values.
        # =====================================================

        ingredient_specs = {

            "rice": {
                "name": "Rice",
                "sku": "RICE-001",
                "category": "Rice & Grains",
                "location": "Dry Store",
                "unit": "G",
                "pack_size": "1000",
                "price": "120",
                "stock": "10000",
                "minimum": "2000",
                "target": "10000",
            },

            "basmati_rice": {
                "name": "Basmati Rice",
                "sku": "RICE-BASMATI",
                "category": "Rice & Grains",
                "location": "Dry Store",
                "unit": "G",
                "pack_size": "1000",
                "price": "180",
                "stock": "10000",
                "minimum": "2000",
                "target": "10000",
            },

            "polao_rice": {
                "name": "Polao Rice",
                "sku": "RICE-POLAO",
                "category": "Rice & Grains",
                "location": "Dry Store",
                "unit": "G",
                "pack_size": "1000",
                "price": "150",
                "stock": "10000",
                "minimum": "2000",
                "target": "10000",
            },

            "chicken": {
                "name": "Chicken",
                "sku": "CHK-001",
                "category": "Meat & Poultry",
                "location": "Freezer",
                "unit": "G",
                "pack_size": "1000",
                "price": "280",
                "stock": "10000",
                "minimum": "2000",
                "target": "10000",
            },

            "beef": {
                "name": "Beef",
                "sku": "BEF-001",
                "category": "Meat & Poultry",
                "location": "Freezer",
                "unit": "G",
                "pack_size": "1000",
                "price": "800",
                "stock": "6000",
                "minimum": "1500",
                "target": "6000",
            },

            "mutton": {
                "name": "Mutton",
                "sku": "MUTTON-001",
                "category": "Meat & Poultry",
                "location": "Freezer",
                "unit": "G",
                "pack_size": "1000",
                "price": "950",
                "stock": "6000",
                "minimum": "1500",
                "target": "6000",
            },

            "hilsa": {
                "name": "Hilsa Fish",
                "sku": "FISH-HILSA",
                "category": "Seafood",
                "location": "Freezer",
                "unit": "G",
                "pack_size": "1000",
                "price": "1400",
                "stock": "4000",
                "minimum": "1000",
                "target": "4000",
                "allergens": [
                    "Fish",
                ],
            },

            "egg": {
                "name": "Egg",
                "sku": "EGG-001",
                "category": "Meat & Poultry",
                "location": "Chiller",
                "unit": "PCS",
                "pack_size": "12",
                "price": "150",
                "stock": "60",
                "minimum": "12",
                "target": "60",
                "allergens": [
                    "Egg",
                ],
            },

            "potato": {
                "name": "Potato",
                "sku": "VEG-POTATO",
                "category": "Vegetables",
                "location": "Dry Store",
                "unit": "G",
                "pack_size": "1000",
                "price": "60",
                "stock": "6000",
                "minimum": "1000",
                "target": "6000",
            },

            "onion": {
                "name": "Onion",
                "sku": "VEG-ONION",
                "category": "Vegetables",
                "location": "Dry Store",
                "unit": "G",
                "pack_size": "1000",
                "price": "120",
                "stock": "5000",
                "minimum": "1000",
                "target": "5000",
            },

            "tomato": {
                "name": "Tomato",
                "sku": "VEG-TOMATO",
                "category": "Vegetables",
                "location": "Chiller",
                "unit": "G",
                "pack_size": "1000",
                "price": "120",
                "stock": "3000",
                "minimum": "500",
                "target": "3000",
            },

            "carrot": {
                "name": "Carrot",
                "sku": "VEG-CARROT",
                "category": "Vegetables",
                "location": "Chiller",
                "unit": "G",
                "pack_size": "1000",
                "price": "120",
                "stock": "3000",
                "minimum": "500",
                "target": "3000",
            },

            "beans": {
                "name": "Beans",
                "sku": "VEG-BEANS",
                "category": "Vegetables",
                "location": "Chiller",
                "unit": "G",
                "pack_size": "1000",
                "price": "180",
                "stock": "2000",
                "minimum": "400",
                "target": "2000",
            },

            "capsicum": {
                "name": "Capsicum",
                "sku": "VEG-CAPSICUM",
                "category": "Vegetables",
                "location": "Chiller",
                "unit": "G",
                "pack_size": "1000",
                "price": "300",
                "stock": "2000",
                "minimum": "400",
                "target": "2000",
            },

            "cabbage": {
                "name": "Cabbage",
                "sku": "VEG-CABBAGE",
                "category": "Vegetables",
                "location": "Chiller",
                "unit": "G",
                "pack_size": "1000",
                "price": "80",
                "stock": "3000",
                "minimum": "500",
                "target": "3000",
            },

            "lettuce": {
                "name": "Lettuce",
                "sku": "VEG-LETTUCE",
                "category": "Vegetables",
                "location": "Chiller",
                "unit": "G",
                "pack_size": "1000",
                "price": "280",
                "stock": "1500",
                "minimum": "300",
                "target": "1500",
            },

            "cucumber": {
                "name": "Cucumber",
                "sku": "VEG-CUCUMBER",
                "category": "Vegetables",
                "location": "Chiller",
                "unit": "G",
                "pack_size": "1000",
                "price": "100",
                "stock": "2000",
                "minimum": "400",
                "target": "2000",
            },

            "green_chili": {
                "name": "Green Chili",
                "sku": "VEG-CHILI",
                "category": "Vegetables",
                "location": "Chiller",
                "unit": "G",
                "pack_size": "1000",
                "price": "200",
                "stock": "1000",
                "minimum": "200",
                "target": "1000",
            },

            "coriander": {
                "name": "Coriander Leaves",
                "sku": "VEG-CORIANDER",
                "category": "Vegetables",
                "location": "Chiller",
                "unit": "G",
                "pack_size": "500",
                "price": "120",
                "stock": "1000",
                "minimum": "200",
                "target": "1000",
            },

            "ginger": {
                "name": "Ginger",
                "sku": "SPICE-GINGER",
                "category": "Spices",
                "location": "Chiller",
                "unit": "G",
                "pack_size": "1000",
                "price": "250",
                "stock": "2000",
                "minimum": "300",
                "target": "2000",
            },

            "garlic": {
                "name": "Garlic",
                "sku": "VEG-GARLIC",
                "category": "Vegetables",
                "location": "Dry Store",
                "unit": "G",
                "pack_size": "1000",
                "price": "250",
                "stock": "2000",
                "minimum": "300",
                "target": "2000",
            },

            "yogurt": {
                "name": "Yogurt",
                "sku": "DAIRY-YOGURT",
                "category": "Dairy",
                "location": "Chiller",
                "unit": "G",
                "pack_size": "1000",
                "price": "180",
                "stock": "3000",
                "minimum": "500",
                "target": "3000",
                "allergens": [
                    "Milk",
                ],
            },

            "milk": {
                "name": "Milk",
                "sku": "DAIRY-MILK",
                "category": "Dairy",
                "location": "Chiller",
                "unit": "ML",
                "pack_size": "1000",
                "price": "110",
                "stock": "5000",
                "minimum": "1000",
                "target": "5000",
                "allergens": [
                    "Milk",
                ],
            },

            "ghee": {
                "name": "Ghee",
                "sku": "DAIRY-GHEE",
                "category": "Dairy",
                "location": "Dry Store",
                "unit": "G",
                "pack_size": "1000",
                "price": "900",
                "stock": "2000",
                "minimum": "400",
                "target": "2000",
                "allergens": [
                    "Milk",
                ],
            },

            "mozzarella": {
                "name": "Mozzarella Cheese",
                "sku": "DAIRY-MOZZARELLA",
                "category": "Dairy",
                "location": "Chiller",
                "unit": "G",
                "pack_size": "1000",
                "price": "850",
                "stock": "2000",
                "minimum": "400",
                "target": "2000",
                "allergens": [
                    "Milk",
                ],
            },

            "cheese_slice": {
                "name": "Cheese Slice",
                "sku": "DAIRY-CHEESE-SLICE",
                "category": "Dairy",
                "location": "Chiller",
                "unit": "PCS",
                "pack_size": "20",
                "price": "400",
                "stock": "100",
                "minimum": "20",
                "target": "100",
                "allergens": [
                    "Milk",
                ],
            },

            "cooking_oil": {
                "name": "Cooking Oil",
                "sku": "OIL-001",
                "category": "Sauces & Condiments",
                "location": "Dry Store",
                "unit": "ML",
                "pack_size": "1000",
                "price": "200",
                "stock": "10000",
                "minimum": "2000",
                "target": "10000",
            },

            "mustard_oil": {
                "name": "Mustard Oil",
                "sku": "OIL-MUSTARD",
                "category": "Sauces & Condiments",
                "location": "Dry Store",
                "unit": "ML",
                "pack_size": "1000",
                "price": "300",
                "stock": "2000",
                "minimum": "400",
                "target": "2000",
                "allergens": [
                    "Mustard",
                ],
            },

            "soy_sauce": {
                "name": "Soy Sauce",
                "sku": "SAUCE-SOY",
                "category": "Sauces & Condiments",
                "location": "Dry Store",
                "unit": "ML",
                "pack_size": "1000",
                "price": "250",
                "stock": "3000",
                "minimum": "500",
                "target": "3000",
                "allergens": [
                    "Soy",
                ],
            },

            "ketchup": {
                "name": "Tomato Ketchup",
                "sku": "SAUCE-KETCHUP",
                "category": "Sauces & Condiments",
                "location": "Dry Store",
                "unit": "ML",
                "pack_size": "1000",
                "price": "250",
                "stock": "3000",
                "minimum": "500",
                "target": "3000",
            },

            "mayonnaise": {
                "name": "Mayonnaise",
                "sku": "SAUCE-MAYO",
                "category": "Sauces & Condiments",
                "location": "Chiller",
                "unit": "ML",
                "pack_size": "1000",
                "price": "420",
                "stock": "2500",
                "minimum": "500",
                "target": "2500",
                "allergens": [
                    "Egg",
                ],
            },

            "garlic_sauce": {
                "name": "Garlic Sauce",
                "sku": "SAUCE-GARLIC",
                "category": "Sauces & Condiments",
                "location": "Chiller",
                "unit": "ML",
                "pack_size": "1000",
                "price": "350",
                "stock": "2000",
                "minimum": "400",
                "target": "2000",
            },

            "bbq_sauce": {
                "name": "BBQ Sauce",
                "sku": "SAUCE-BBQ",
                "category": "Sauces & Condiments",
                "location": "Dry Store",
                "unit": "ML",
                "pack_size": "1000",
                "price": "400",
                "stock": "2000",
                "minimum": "400",
                "target": "2000",
            },

            "flour": {
                "name": "Flour",
                "sku": "DRY-FLOUR",
                "category": "Dry Ingredients",
                "location": "Dry Store",
                "unit": "G",
                "pack_size": "1000",
                "price": "70",
                "stock": "5000",
                "minimum": "1000",
                "target": "5000",
                "allergens": [
                    "Gluten",
                ],
            },

            "salt": {
                "name": "Salt",
                "sku": "DRY-SALT",
                "category": "Spices",
                "location": "Dry Store",
                "unit": "G",
                "pack_size": "1000",
                "price": "50",
                "stock": "3000",
                "minimum": "500",
                "target": "3000",
            },

            "sugar": {
                "name": "Sugar",
                "sku": "DRY-SUGAR",
                "category": "Dry Ingredients",
                "location": "Dry Store",
                "unit": "G",
                "pack_size": "1000",
                "price": "140",
                "stock": "5000",
                "minimum": "1000",
                "target": "5000",
            },

            "black_pepper": {
                "name": "Black Pepper",
                "sku": "SPICE-PEPPER",
                "category": "Spices",
                "location": "Dry Store",
                "unit": "G",
                "pack_size": "100",
                "price": "180",
                "stock": "500",
                "minimum": "100",
                "target": "500",
            },

            "biryani_masala": {
                "name": "Biryani Masala",
                "sku": "SPICE-BIRYANI",
                "category": "Spices",
                "location": "Dry Store",
                "unit": "G",
                "pack_size": "500",
                "price": "350",
                "stock": "1500",
                "minimum": "300",
                "target": "1500",
            },

            "tehari_masala": {
                "name": "Tehari Masala",
                "sku": "SPICE-TEHARI",
                "category": "Spices",
                "location": "Dry Store",
                "unit": "G",
                "pack_size": "500",
                "price": "320",
                "stock": "1000",
                "minimum": "200",
                "target": "1000",
            },

            "curry_masala": {
                "name": "Curry Masala",
                "sku": "SPICE-CURRY",
                "category": "Spices",
                "location": "Dry Store",
                "unit": "G",
                "pack_size": "500",
                "price": "300",
                "stock": "1000",
                "minimum": "200",
                "target": "1000",
            },

            "kebab_masala": {
                "name": "Kebab Masala",
                "sku": "SPICE-KEBAB",
                "category": "Spices",
                "location": "Dry Store",
                "unit": "G",
                "pack_size": "500",
                "price": "380",
                "stock": "1000",
                "minimum": "200",
                "target": "1000",
            },

            "tikka_masala": {
                "name": "Tikka Masala",
                "sku": "SPICE-TIKKA",
                "category": "Spices",
                "location": "Dry Store",
                "unit": "G",
                "pack_size": "500",
                "price": "380",
                "stock": "1000",
                "minimum": "200",
                "target": "1000",
            },

            "chaap_masala": {
                "name": "Chaap Masala",
                "sku": "SPICE-CHAAP",
                "category": "Spices",
                "location": "Dry Store",
                "unit": "G",
                "pack_size": "500",
                "price": "400",
                "stock": "1000",
                "minimum": "200",
                "target": "1000",
            },

            "tandoori_masala": {
                "name": "Tandoori Masala",
                "sku": "SPICE-TANDOORI",
                "category": "Spices",
                "location": "Dry Store",
                "unit": "G",
                "pack_size": "500",
                "price": "380",
                "stock": "1000",
                "minimum": "200",
                "target": "1000",
            },

            "mustard_paste": {
                "name": "Mustard Paste",
                "sku": "SAUCE-MUSTARD",
                "category": "Sauces & Condiments",
                "location": "Chiller",
                "unit": "G",
                "pack_size": "500",
                "price": "220",
                "stock": "1000",
                "minimum": "200",
                "target": "1000",
                "allergens": [
                    "Mustard",
                ],
            },

            "cashew_paste": {
                "name": "Cashew Paste",
                "sku": "DRY-CASHEW",
                "category": "Dry Ingredients",
                "location": "Dry Store",
                "unit": "G",
                "pack_size": "500",
                "price": "700",
                "stock": "1000",
                "minimum": "200",
                "target": "1000",
                "allergens": [
                    "Tree Nuts",
                ],
            },

            "cardamom": {
                "name": "Cardamom",
                "sku": "SPICE-CARDAMOM",
                "category": "Spices",
                "location": "Dry Store",
                "unit": "G",
                "pack_size": "100",
                "price": "350",
                "stock": "300",
                "minimum": "50",
                "target": "300",
            },

            "tea_leaves": {
                "name": "Tea Leaves",
                "sku": "BEV-TEA",
                "category": "Beverages",
                "location": "Dry Store",
                "unit": "G",
                "pack_size": "500",
                "price": "300",
                "stock": "1000",
                "minimum": "200",
                "target": "1000",
            },

            "mint": {
                "name": "Mint Leaves",
                "sku": "VEG-MINT",
                "category": "Vegetables",
                "location": "Chiller",
                "unit": "G",
                "pack_size": "500",
                "price": "150",
                "stock": "800",
                "minimum": "150",
                "target": "800",
            },

            "roasted_cumin": {
                "name": "Roasted Cumin",
                "sku": "SPICE-CUMIN",
                "category": "Spices",
                "location": "Dry Store",
                "unit": "G",
                "pack_size": "500",
                "price": "300",
                "stock": "800",
                "minimum": "150",
                "target": "800",
            },

            "mango_pulp": {
                "name": "Mango Pulp",
                "sku": "BEV-MANGO-PULP",
                "category": "Beverages",
                "location": "Chiller",
                "unit": "G",
                "pack_size": "1000",
                "price": "300",
                "stock": "3000",
                "minimum": "500",
                "target": "3000",
            },

            "noodles": {
                "name": "Chow Mein Noodles",
                "sku": "NOODLE-001",
                "category": "Noodles & Pasta",
                "location": "Dry Store",
                "unit": "G",
                "pack_size": "1000",
                "price": "180",
                "stock": "4000",
                "minimum": "800",
                "target": "4000",
                "allergens": [
                    "Gluten",
                ],
            },

            "burger_bun": {
                "name": "Burger Bun",
                "sku": "BAKERY-BUN",
                "category": "Bakery",
                "location": "Dry Store",
                "unit": "PCS",
                "pack_size": "6",
                "price": "120",
                "stock": "60",
                "minimum": "12",
                "target": "60",
                "allergens": [
                    "Gluten",
                ],
            },

            "flatbread": {
                "name": "Shawarma Bread",
                "sku": "BAKERY-SHAWARMA",
                "category": "Bakery",
                "location": "Dry Store",
                "unit": "PCS",
                "pack_size": "10",
                "price": "180",
                "stock": "80",
                "minimum": "20",
                "target": "80",
                "allergens": [
                    "Gluten",
                ],
            },

            "pizza_dough": {
                "name": "Pizza Dough",
                "sku": "BAKERY-PIZZA-DOUGH",
                "category": "Bakery",
                "location": "Chiller",
                "unit": "G",
                "pack_size": "1000",
                "price": "220",
                "stock": "5000",
                "minimum": "1000",
                "target": "5000",
                "allergens": [
                    "Gluten",
                ],
            },

            "milk_powder": {
                "name": "Milk Powder",
                "sku": "DESSERT-MILK-POWDER",
                "category": "Dessert Ingredients",
                "location": "Dry Store",
                "unit": "G",
                "pack_size": "1000",
                "price": "750",
                "stock": "2000",
                "minimum": "400",
                "target": "2000",
                "allergens": [
                    "Milk",
                ],
            },

            "chhana": {
                "name": "Chhana",
                "sku": "DESSERT-CHHANA",
                "category": "Dessert Ingredients",
                "location": "Chiller",
                "unit": "G",
                "pack_size": "1000",
                "price": "550",
                "stock": "2000",
                "minimum": "400",
                "target": "2000",
                "allergens": [
                    "Milk",
                ],
            },

            "pistachio": {
                "name": "Pistachio",
                "sku": "DESSERT-PISTACHIO",
                "category": "Dessert Ingredients",
                "location": "Dry Store",
                "unit": "G",
                "pack_size": "500",
                "price": "1800",
                "stock": "500",
                "minimum": "100",
                "target": "500",
                "allergens": [
                    "Tree Nuts",
                ],
            },

            "cola_drink": {
                "name": "Carbonated Cola Drink",
                "sku": "DRINK-COLA",
                "category": "Beverages",
                "location": "Beverage Store",
                "unit": "PCS",
                "pack_size": "1",
                "price": "25",
                "stock": "100",
                "minimum": "20",
                "target": "100",
            },
        }

        # =====================================================
        # SAFE INGREDIENT CREATION
        # =====================================================

        ingredient_objects = {}


        def resolve_ingredient(key):

            if key in ingredient_objects:
                return ingredient_objects[key]

            spec = ingredient_specs[key]

            # Prefer exact ingredient name first.
            obj = Ingredient.objects.filter(
                restaurant=restaurant,
                name__iexact=spec["name"],
            ).first()

            # Then reuse same SKU if the restaurant
            # already has an equivalent ingredient.
            if not obj:
                obj = Ingredient.objects.filter(
                    restaurant=restaurant,
                    sku=spec["sku"],
                ).first()

            if obj:

                if obj.base_unit != spec["unit"]:
                    raise CommandError(
                        f"Unit mismatch for ingredient "
                        f"'{obj.name}'. Existing unit is "
                        f"{obj.base_unit}, recipe expects "
                        f"{spec['unit']}."
                    )

                if not obj.is_active:
                    obj.is_active = True

                    obj.save(
                        update_fields=[
                            "is_active",
                        ]
                    )

            else:

                obj = Ingredient.objects.create(
                    restaurant=restaurant,

                    category=categories[
                        spec["category"]
                    ],

                    storage_location=locations[
                        spec["location"]
                    ],

                    name=spec["name"],

                    sku=spec["sku"],

                    base_unit=spec["unit"],

                    pack_size=Decimal(
                        spec["pack_size"]
                    ),

                    current_pack_price=Decimal(
                        spec["price"]
                    ),

                    current_stock=Decimal(
                        spec["stock"]
                    ),

                    minimum_level=Decimal(
                        spec["minimum"]
                    ),

                    target_level=Decimal(
                        spec["target"]
                    ),

                    is_active=True,
                )

                self.stdout.write(
                    f"Created ingredient: "
                    f"{obj.name}"
                )

            for allergen_name in spec.get(
                "allergens",
                [],
            ):

                allergen, _ = (
                    Allergen.objects.get_or_create(
                        name=allergen_name
                    )
                )

                obj.allergens.add(
                    allergen
                )

            ingredient_objects[
                key
            ] = obj

            return obj


        # =====================================================
        # COMPLETE FOOD CATALOG
        #
        # recipe row format:
        # ingredient_key, quantity, customer_visible
        # =====================================================

        recipes = [

            {
                "name": "Mutton Kacchi Biryani",
                "aliases": [
                    "Mutton Kacchi Biryani",
                ],
                "description": (
                    "Traditional kacchi biryani with aromatic "
                    "basmati rice, tender marinated mutton, "
                    "potato and fragrant spices."
                ),
                "ingredients": [
                    ("basmati_rice", 250, True),
                    ("mutton", 180, True),
                    ("potato", 100, True),
                    ("yogurt", 40, True),
                    ("onion", 50, True),
                    ("ghee", 15, False),
                    ("cooking_oil", 20, False),
                    ("ginger", 10, False),
                    ("garlic", 10, False),
                    ("biryani_masala", 12, False),
                    ("salt", 3, False),
                ],
            },

            {
                "name": "Chicken Biryani",
                "aliases": [
                    "Chicken Biryani",
                ],
                "description": (
                    "Aromatic basmati rice cooked with "
                    "seasoned chicken, potato and traditional "
                    "biryani spices."
                ),
                "ingredients": [
                    ("basmati_rice", 250, True),
                    ("chicken", 170, True),
                    ("potato", 80, True),
                    ("yogurt", 35, True),
                    ("onion", 45, True),
                    ("ghee", 10, False),
                    ("cooking_oil", 20, False),
                    ("ginger", 8, False),
                    ("garlic", 8, False),
                    ("biryani_masala", 10, False),
                    ("salt", 3, False),
                ],
            },

            {
                "name": "Beef Tehari",
                "aliases": [
                    "Beef Tehari",
                ],
                "description": (
                    "Traditional beef tehari made with "
                    "fragrant polao rice, tender beef, "
                    "potato and green chili."
                ),
                "ingredients": [
                    ("polao_rice", 250, True),
                    ("beef", 160, True),
                    ("potato", 70, True),
                    ("onion", 50, True),
                    ("green_chili", 6, True),
                    ("yogurt", 25, False),
                    ("cooking_oil", 25, False),
                    ("ginger", 8, False),
                    ("garlic", 8, False),
                    ("tehari_masala", 10, False),
                    ("salt", 3, False),
                ],
            },

            {
                "name": "Morog Polao",
                "aliases": [
                    "Morog Polao",
                ],
                "description": (
                    "Classic Bangladeshi morog polao with "
                    "fragrant polao rice and tender chicken "
                    "cooked in a mildly rich spice blend."
                ),
                "ingredients": [
                    ("polao_rice", 250, True),
                    ("chicken", 180, True),
                    ("onion", 50, True),
                    ("yogurt", 30, True),
                    ("milk", 40, True),
                    ("ghee", 15, False),
                    ("ginger", 8, False),
                    ("garlic", 8, False),
                    ("cardamom", 1, False),
                    ("salt", 3, False),
                ],
            },

            {
                "name": "Steamed Rice",
                "aliases": [
                    "Steamed Rice",
                    "SteamedRice",
                ],
                "description": (
                    "Freshly steamed rice served hot, "
                    "perfect with curries and main dishes."
                ),
                "ingredients": [
                    ("rice", 250, True),
                ],
            },

            {
                "name": "Chicken Kacchi",
                "aliases": [
                    "Chicken Kacchi",
                    "chiken kacchi",
                ],
                "description": (
                    "Kacchi-style aromatic rice prepared "
                    "with marinated chicken, potato, yogurt "
                    "and traditional spices."
                ),
                "ingredients": [
                    ("basmati_rice", 250, True),
                    ("chicken", 170, True),
                    ("potato", 90, True),
                    ("yogurt", 35, True),
                    ("onion", 45, True),
                    ("ghee", 12, False),
                    ("cooking_oil", 18, False),
                    ("biryani_masala", 10, False),
                    ("salt", 3, False),
                ],
            },

            {
                "name": "Chicken Chow Mein",
                "aliases": [
                    "Chicken Chow Mein",
                ],
                "description": (
                    "Stir-fried chow mein noodles with "
                    "chicken, cabbage, carrot and capsicum "
                    "in a savory sauce."
                ),
                "ingredients": [
                    ("noodles", 180, True),
                    ("chicken", 80, True),
                    ("cabbage", 40, True),
                    ("carrot", 30, True),
                    ("capsicum", 20, True),
                    ("soy_sauce", 15, False),
                    ("cooking_oil", 15, False),
                    ("black_pepper", 1, False),
                ],
            },

            {
                "name": "Beef Bhuna",
                "aliases": [
                    "Beef Bhuna",
                ],
                "description": (
                    "Slow-cooked beef bhuna with onion, "
                    "tomato and rich traditional spices."
                ),
                "ingredients": [
                    ("beef", 180, True),
                    ("onion", 70, True),
                    ("tomato", 50, True),
                    ("yogurt", 25, True),
                    ("ginger", 10, False),
                    ("garlic", 10, False),
                    ("cooking_oil", 20, False),
                    ("curry_masala", 10, False),
                    ("salt", 3, False),
                ],
            },

            {
                "name": "Chicken Curry",
                "aliases": [
                    "Chicken Curry",
                ],
                "description": (
                    "Homestyle chicken curry cooked with "
                    "potato, onion, tomato and aromatic spices."
                ),
                "ingredients": [
                    ("chicken", 180, True),
                    ("potato", 80, True),
                    ("onion", 60, True),
                    ("tomato", 50, True),
                    ("yogurt", 20, True),
                    ("ginger", 8, False),
                    ("garlic", 8, False),
                    ("cooking_oil", 18, False),
                    ("curry_masala", 8, False),
                    ("salt", 3, False),
                ],
            },

            {
                "name": "Ilish Bhapa",
                "aliases": [
                    "Ilish Bhapa",
                ],
                "description": (
                    "Hilsa fish steamed in a traditional "
                    "mustard sauce with green chili."
                ),
                "ingredients": [
                    ("hilsa", 180, True),
                    ("mustard_paste", 25, True),
                    ("green_chili", 8, True),
                    ("mustard_oil", 15, False),
                    ("salt", 2, False),
                ],
            },

            {
                "name": "Mutton Rezala",
                "aliases": [
                    "Mutton Rezala",
                ],
                "description": (
                    "Tender mutton cooked in a mild, creamy "
                    "yogurt-based rezala gravy with aromatic "
                    "spices."
                ),
                "ingredients": [
                    ("mutton", 180, True),
                    ("yogurt", 50, True),
                    ("onion", 50, True),
                    ("milk", 40, True),
                    ("cashew_paste", 20, True),
                    ("ghee", 10, False),
                    ("ginger", 8, False),
                    ("garlic", 8, False),
                    ("cardamom", 1, False),
                ],
            },

            {
                "name": "Chicken Tikka",
                "aliases": [
                    "Chicken Tikka",
                ],
                "description": (
                    "Boneless chicken marinated with yogurt "
                    "and tikka spices, then grilled until "
                    "smoky and tender."
                ),
                "ingredients": [
                    ("chicken", 160, True),
                    ("yogurt", 40, True),
                    ("ginger", 8, False),
                    ("garlic", 8, False),
                    ("tikka_masala", 10, False),
                    ("cooking_oil", 10, False),
                ],
            },

            {
                "name": "Beef Seekh Kebab",
                "aliases": [
                    "Beef Seekh Kebab",
                ],
                "description": (
                    "Seasoned minced beef seekh kebab "
                    "grilled with onion, green chili and "
                    "fresh coriander."
                ),
                "ingredients": [
                    ("beef", 150, True),
                    ("onion", 30, True),
                    ("green_chili", 5, True),
                    ("coriander", 8, True),
                    ("ginger", 5, False),
                    ("garlic", 5, False),
                    ("kebab_masala", 8, False),
                    ("cooking_oil", 8, False),
                ],
            },

            {
                "name": "Mutton Chaap",
                "aliases": [
                    "Mutton Chaap",
                ],
                "description": (
                    "Tender mutton chaap marinated with "
                    "yogurt and spices, cooked until rich "
                    "and flavorful."
                ),
                "ingredients": [
                    ("mutton", 180, True),
                    ("yogurt", 40, True),
                    ("onion", 40, True),
                    ("ginger", 8, False),
                    ("garlic", 8, False),
                    ("chaap_masala", 10, False),
                    ("cooking_oil", 15, False),
                ],
            },

            {
                "name": "Tandoori Chicken",
                "aliases": [
                    "Tandoori Chicken",
                ],
                "description": (
                    "Chicken marinated with yogurt and "
                    "tandoori spices, grilled for a smoky "
                    "charred finish."
                ),
                "ingredients": [
                    ("chicken", 200, True),
                    ("yogurt", 50, True),
                    ("ginger", 8, False),
                    ("garlic", 8, False),
                    ("tandoori_masala", 12, False),
                    ("cooking_oil", 10, False),
                ],
            },

            {
                "name": "Chicken Burger",
                "aliases": [
                    "Chicken Burger",
                ],
                "description": (
                    "Juicy chicken burger with a soft bun, "
                    "fresh lettuce, tomato, onion and creamy "
                    "sauce."
                ),
                "ingredients": [
                    ("burger_bun", 1, True),
                    ("chicken", 140, True),
                    ("lettuce", 20, True),
                    ("tomato", 25, True),
                    ("onion", 15, True),
                    ("mayonnaise", 15, False),
                    ("ketchup", 10, False),
                    ("cooking_oil", 10, False),
                ],
            },

            {
                "name": "Beef Burger",
                "aliases": [
                    "Beef Burger",
                ],
                "description": (
                    "Grilled beef burger with a soft bun, "
                    "cheese, lettuce, tomato, onion and "
                    "house sauces."
                ),
                "ingredients": [
                    ("burger_bun", 1, True),
                    ("beef", 150, True),
                    ("cheese_slice", 1, True),
                    ("lettuce", 20, True),
                    ("tomato", 25, True),
                    ("onion", 15, True),
                    ("mayonnaise", 15, False),
                    ("ketchup", 10, False),
                    ("cooking_oil", 10, False),
                ],
            },

            {
                "name": "Chicken Shawarma",
                "aliases": [
                    "Chicken Shawarma",
                ],
                "description": (
                    "Seasoned chicken wrapped in soft "
                    "shawarma bread with fresh vegetables "
                    "and creamy garlic sauce."
                ),
                "ingredients": [
                    ("flatbread", 1, True),
                    ("chicken", 120, True),
                    ("cabbage", 25, True),
                    ("cucumber", 25, True),
                    ("onion", 15, True),
                    ("garlic_sauce", 20, False),
                    ("mayonnaise", 10, False),
                ],
            },

            {
                "name": "French Fries",
                "aliases": [
                    "French Fries",
                ],
                "description": (
                    "Crispy golden potato fries served "
                    "fresh and lightly seasoned."
                ),
                "ingredients": [
                    ("potato", 180, True),
                    ("cooking_oil", 20, False),
                    ("salt", 2, False),
                ],
            },

            {
                "name": "BBQ Pizza",
                "aliases": [
                    "BBQ Pizza",
                ],
                "description": (
                    "BBQ chicken pizza topped with "
                    "mozzarella cheese, onion, capsicum "
                    "and smoky barbecue sauce."
                ),
                "ingredients": [
                    ("pizza_dough", 250, True),
                    ("chicken", 100, True),
                    ("mozzarella", 90, True),
                    ("onion", 25, True),
                    ("capsicum", 25, True),
                    ("bbq_sauce", 35, False),
                ],
            },

            {
                "name": "Borhani",
                "aliases": [
                    "Borhani",
                ],
                "description": (
                    "Traditional chilled yogurt drink "
                    "flavored with mint and roasted cumin."
                ),
                "ingredients": [
                    ("yogurt", 180, True),
                    ("mint", 5, True),
                    ("roasted_cumin", 3, True),
                    ("sugar", 8, False),
                    ("salt", 2, False),
                ],
            },

            {
                "name": "Mango Lassi",
                "aliases": [
                    "Mango Lassi",
                ],
                "description": (
                    "Creamy chilled mango lassi blended "
                    "with yogurt, mango and milk."
                ),
                "ingredients": [
                    ("yogurt", 180, True),
                    ("mango_pulp", 120, True),
                    ("milk", 80, True),
                    ("sugar", 20, False),
                ],
            },

            {
                "name": "Masala Tea",
                "aliases": [
                    "Masala Tea",
                ],
                "description": (
                    "Hot milk tea brewed with tea leaves, "
                    "ginger and aromatic cardamom."
                ),
                "ingredients": [
                    ("milk", 120, True),
                    ("tea_leaves", 8, True),
                    ("ginger", 5, True),
                    ("cardamom", 1, True),
                    ("sugar", 15, False),
                ],
            },

            {
                "name": "Mojo",
                "aliases": [
                    "Mojo",
                ],
                "description": (
                    "Chilled carbonated cola soft drink."
                ),
                "ingredients": [
                    ("cola_drink", 1, True),
                ],
            },

            {
                "name": "Firni",
                "aliases": [
                    "Firni",
                ],
                "description": (
                    "Traditional creamy rice pudding "
                    "prepared with milk, rice, sugar and "
                    "aromatic cardamom."
                ),
                "ingredients": [
                    ("rice", 40, True),
                    ("milk", 300, True),
                    ("sugar", 35, True),
                    ("cardamom", 1, True),
                    ("pistachio", 5, True),
                ],
            },

            {
                "name": "Gulab Jamun",
                "aliases": [
                    "Gulab Jamun",
                ],
                "description": (
                    "Soft milk-based dumplings soaked "
                    "in a fragrant sugar syrup."
                ),
                "ingredients": [
                    ("milk_powder", 60, True),
                    ("flour", 15, True),
                    ("sugar", 80, True),
                    ("cardamom", 1, True),
                    ("cooking_oil", 20, False),
                ],
            },

            {
                "name": "Roshmalai",
                "aliases": [
                    "Roshmalai",
                ],
                "description": (
                    "Soft chhana dumplings served in "
                    "sweetened, cardamom-flavored milk."
                ),
                "ingredients": [
                    ("chhana", 100, True),
                    ("milk", 300, True),
                    ("sugar", 40, True),
                    ("cardamom", 1, True),
                    ("pistachio", 5, True),
                ],
            },
        ]

        # =====================================================
        # BUILD / UPDATE RECIPES
        # =====================================================

        total_food_records_updated = 0


        for spec in recipes:

            name_query = Q()

            for alias in spec["aliases"]:
                name_query |= Q(
                    name__iexact=alias
                )

            items = (
                MenuItem.objects.filter(
                    name_query,
                    category__restaurant=restaurant,
                    is_available=True,
                    item_type=MenuItem.ItemType.NORMAL,
                )
                .select_related(
                    "category"
                )
                .order_by(
                    "id"
                )
            )

            if not items.exists():

                self.stdout.write(
                    self.style.WARNING(
                        f"Not found: {spec['name']}"
                    )
                )

                continue


            for item in items:

                # ---------------------------------------------
                # FIX "chiken kacchi" TYPO SAFELY
                # ---------------------------------------------

                if (
                    spec["name"] ==
                    "Chicken Kacchi"
                    and
                    item.name.lower() ==
                    "chiken kacchi"
                ):

                    canonical_exists = (
                        MenuItem.objects.filter(
                            category__restaurant=restaurant,
                            name__iexact="Chicken Kacchi",
                        )
                        .exclude(
                            pk=item.pk
                        )
                        .exists()
                    )

                    if not canonical_exists:
                        item.name = (
                            "Chicken Kacchi"
                        )

                # ---------------------------------------------
                # CUSTOMER DESCRIPTION
                # ---------------------------------------------

                item.description = (
                    spec["description"]
                )

                item.save(
                    update_fields=[
                        "name",
                        "description",
                    ]
                )

                # ---------------------------------------------
                # RECIPE
                # ---------------------------------------------

                recipe, _ = (
                    Recipe.objects.update_or_create(
                        menu_item=item,
                        defaults={
                            "yield_quantity":
                                Decimal("1.00"),

                            "instructions":
                                (
                                    "Prepare according to "
                                    "the restaurant's standard "
                                    "kitchen procedure."
                                ),
                        },
                    )
                )

                used_ingredient_ids = []


                for (
                    ingredient_key,
                    quantity,
                    customer_visible,
                ) in spec["ingredients"]:

                    ingredient_obj = (
                        resolve_ingredient(
                            ingredient_key
                        )
                    )

                    RecipeIngredient.objects.update_or_create(
                        recipe=recipe,
                        ingredient=ingredient_obj,
                        defaults={
                            "quantity":
                                Decimal(
                                    str(quantity)
                                ),

                            "is_customer_visible":
                                customer_visible,
                        },
                    )

                    used_ingredient_ids.append(
                        ingredient_obj.id
                    )


                # Remove old ingredients from THIS recipe
                # that are not part of the standardized recipe.

                RecipeIngredient.objects.filter(
                    recipe=recipe
                ).exclude(
                    ingredient_id__in=
                        used_ingredient_ids
                ).delete()


                total_food_records_updated += 1


                self.stdout.write(
                    self.style.SUCCESS(
                        f"Ready: #{item.id} "
                        f"{item.name} "
                        f"({item.category.name})"
                    )
                )

        # =====================================================
        # FINAL DATABASE AUDIT
        # =====================================================

        active_normal = MenuItem.objects.filter(
            category__restaurant=restaurant,
            is_available=True,
            item_type=MenuItem.ItemType.NORMAL,
        )


        missing_recipe = (
            active_normal.filter(
                recipe__isnull=True
            )
            .distinct()
            .order_by(
                "category__name",
                "name",
            )
        )


        missing_description = (
            active_normal.filter(
                description=""
            )
            .distinct()
            .order_by(
                "category__name",
                "name",
            )
        )


        no_visible_ingredient = (
            active_normal.filter(
                recipe__isnull=False
            )
            .exclude(
                recipe__recipe_ingredients__is_customer_visible=True
            )
            .distinct()
            .order_by(
                "category__name",
                "name",
            )
        )


        set_menu_without_components = (
            MenuItem.objects.filter(
                category__restaurant=restaurant,
                is_available=True,
                item_type=MenuItem.ItemType.SET_MENU,
                set_components__isnull=True,
            )
            .distinct()
        )


        def display_queryset(queryset):

            values = queryset.values_list(
                "id",
                "category__name",
                "name",
            )

            rows = [
                f"#{item_id} | "
                f"{category_name} | "
                f"{name}"

                for (
                    item_id,
                    category_name,
                    name,
                ) in values
            ]

            return (
                "\n".join(rows)
                if rows
                else "NONE"
            )


        self.stdout.write("")
        self.stdout.write(
            "=" * 55
        )

        self.stdout.write(
            self.style.SUCCESS(
                "COMPLETE FOOD CATALOG BUILD FINISHED"
            )
        )

        self.stdout.write(
            "=" * 55
        )

        self.stdout.write(
            f"Restaurant: "
            f"{restaurant.name}"
        )

        self.stdout.write(
            f"Food records updated: "
            f"{total_food_records_updated}"
        )

        self.stdout.write(
            f"Active normal foods: "
            f"{active_normal.count()}"
        )

        self.stdout.write("")

        self.stdout.write(
            "=== MISSING RECIPE ==="
        )

        self.stdout.write(
            display_queryset(
                missing_recipe
            )
        )

        self.stdout.write("")

        self.stdout.write(
            "=== MISSING DESCRIPTION ==="
        )

        self.stdout.write(
            display_queryset(
                missing_description
            )
        )

        self.stdout.write("")

        self.stdout.write(
            "=== NO CUSTOMER-VISIBLE INGREDIENT ==="
        )

        self.stdout.write(
            display_queryset(
                no_visible_ingredient
            )
        )

        self.stdout.write("")

        self.stdout.write(
            "=== SET MENU WITHOUT COMPONENTS ==="
        )

        self.stdout.write(
            display_queryset(
                set_menu_without_components
            )
        )

        self.stdout.write("")

        if (
            not missing_recipe.exists()
            and
            not missing_description.exists()
            and
            not no_visible_ingredient.exists()
            and
            not set_menu_without_components.exists()
        ):

            self.stdout.write(
                self.style.SUCCESS(
                    "ALL ACTIVE MENU ITEMS ARE COMPLETE."
                )
            )

        else:

            self.stdout.write(
                self.style.WARNING(
                    "Some items still need attention. "
                    "See the audit above."
                )
            )