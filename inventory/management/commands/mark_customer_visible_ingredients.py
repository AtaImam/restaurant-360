from django.core.management.base import BaseCommand

from inventory.models import RecipeIngredient


class Command(BaseCommand):
    help = (
        "Mark customer-facing ingredients "
        "for menu item details."
    )

    def handle(self, *args, **options):

        visible_rules = {

            "Chicken Fried Rice": [
                "Rice",
                "Chicken",
                "Egg",
                "Carrot",
                "Beans",
                "Onion",
            ],

            "Egg Fried Rice": [
                "Rice",
                "Egg",
                "Carrot",
                "Beans",
                "Onion",
            ],

            "Fried Chicken": [
                "Chicken",
                "Flour",
                "Egg",
                "Black Pepper",
            ],

            "Chinese Vegetable": [
                "Cabbage",
                "Carrot",
                "Capsicum",
                "Beans",
                "Onion",
            ],

            "Chicken Chili Onion": [
                "Chicken",
                "Onion",
                "Capsicum",
                "Green Chili",
            ],

            "Beef Chili Onion": [
                "Beef",
                "Onion",
                "Capsicum",
                "Green Chili",
            ],

            "Chicken Manchurian": [
                "Chicken",
                "Garlic",
            ],

            "Thai Soup": [
                "Chicken",
                "Egg",
                "Green Chili",
            ],

            "Fried Wonton": [
                "Chicken",
                "Cabbage",
                "Onion",
            ],

            "Soft Drink": [
                "Soft Drink Bottle",
            ],
        }

        RecipeIngredient.objects.update(
            is_customer_visible=False
        )

        total_marked = 0


        for (
            menu_item_name,
            ingredient_names
        ) in visible_rules.items():

            updated = (
                RecipeIngredient.objects.filter(
                    recipe__menu_item__name=
                        menu_item_name,

                    ingredient__name__in=
                        ingredient_names,
                )
                .update(
                    is_customer_visible=True
                )
            )

            total_marked += updated

            self.stdout.write(
                f"{menu_item_name}: "
                f"{updated} visible ingredients"
            )


        self.stdout.write("")

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Total visible ingredients: "
                f"{total_marked}"
            )
        )