from decimal import Decimal

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from menu.models import Category, MenuItem
from restaurant.models import Restaurant


class Command(BaseCommand):
    help = "Build realistic Chinese foods and set menus for Restaurant 360."

    @transaction.atomic
    def handle(self, *args, **options):

        restaurant = Restaurant.objects.first()

        if not restaurant:
            raise CommandError(
                "No restaurant found. Create a restaurant first."
            )

        # -----------------------------------------
        # CHECK NEW MENU ARCHITECTURE
        # -----------------------------------------

        if not hasattr(MenuItem, "ItemType"):
            raise CommandError(
                "MenuItem.item_type is missing. "
                "Complete the Menu model upgrade first."
            )

        try:
            SetMenuComponent = apps.get_model(
                "menu",
                "SetMenuComponent",
            )

        except LookupError:
            raise CommandError(
                "SetMenuComponent model not found. "
                "Complete the Menu model migration first."
            )


        # -----------------------------------------
        # HELPERS
        # -----------------------------------------

        def get_category(name):

            category = Category.objects.filter(
                restaurant=restaurant,
                name=name,
            ).first()

            if not category:
                category = Category.objects.create(
                    restaurant=restaurant,
                    name=name,
                )

            return category


        def save_food(
            name,
            category,
            price,
            description,
            item_type,
            serves=1,
        ):

            item = MenuItem.objects.filter(
                category__restaurant=restaurant,
                name=name,
            ).first()

            if not item:
                item = MenuItem()

            item.category = category
            item.name = name
            item.description = description
            item.price = Decimal(str(price))
            item.item_type = item_type
            item.serves = serves
            item.is_available = True

            item.save()

            return item


        # -----------------------------------------
        # CATEGORIES
        # -----------------------------------------

        chinese_category = get_category(
            "Chinese"
        )

        drinks_category = get_category(
            "Drinks"
        )

        set_menu_category = get_category(
            "Set Menu"
        )


        # -----------------------------------------
        # ARCHIVE OLD DEMO SET MENUS
        # -----------------------------------------

        old_demo_names = [
            "Family Platter (4 persons)",
            "Kacchi Combo (1 person)",
            "Set Menu Special",
        ]

        archived_count = (
            MenuItem.objects.filter(
                category__restaurant=restaurant,
                name__in=old_demo_names,
            )
            .update(
                is_available=False
            )
        )


        # -----------------------------------------
        # REAL CHINESE MENU ITEMS
        # -----------------------------------------

        chicken_fried_rice = save_food(
            name="Chicken Fried Rice",
            category=chinese_category,
            price="190.00",
            description=(
                "Wok-fried rice prepared with chicken, "
                "egg and fresh vegetables."
            ),
            item_type=MenuItem.ItemType.NORMAL,
        )


        egg_fried_rice = save_food(
            name="Egg Fried Rice",
            category=chinese_category,
            price="160.00",
            description=(
                "Classic fried rice with egg, "
                "vegetables and light seasoning."
            ),
            item_type=MenuItem.ItemType.NORMAL,
        )


        fried_chicken = save_food(
            name="Fried Chicken",
            category=chinese_category,
            price="110.00",
            description=(
                "Crispy fried chicken with "
                "restaurant-style seasoning."
            ),
            item_type=MenuItem.ItemType.NORMAL,
        )


        chinese_vegetable = save_food(
            name="Chinese Vegetable",
            category=chinese_category,
            price="120.00",
            description=(
                "Fresh mixed vegetables cooked "
                "in a light Chinese-style sauce."
            ),
            item_type=MenuItem.ItemType.NORMAL,
        )


        chicken_chili_onion = save_food(
            name="Chicken Chili Onion",
            category=chinese_category,
            price="190.00",
            description=(
                "Tender chicken cooked with onion, "
                "capsicum and chili sauce."
            ),
            item_type=MenuItem.ItemType.NORMAL,
        )


        beef_chili_onion = save_food(
            name="Beef Chili Onion",
            category=chinese_category,
            price="230.00",
            description=(
                "Sliced beef cooked with onion, "
                "capsicum and savory chili sauce."
            ),
            item_type=MenuItem.ItemType.NORMAL,
        )


        chicken_manchurian = save_food(
            name="Chicken Manchurian",
            category=chinese_category,
            price="220.00",
            description=(
                "Chicken cooked in a sweet, savory "
                "and mildly spicy Manchurian sauce."
            ),
            item_type=MenuItem.ItemType.NORMAL,
        )


        thai_soup = save_food(
            name="Thai Soup",
            category=chinese_category,
            price="160.00",
            description=(
                "Hot and tangy Thai-style soup "
                "with chicken and vegetables."
            ),
            item_type=MenuItem.ItemType.NORMAL,
        )


        wonton = save_food(
            name="Fried Wonton",
            category=chinese_category,
            price="150.00",
            description=(
                "Crispy fried wontons served "
                "with house dipping sauce."
            ),
            item_type=MenuItem.ItemType.NORMAL,
        )


        soft_drink = save_food(
            name="Soft Drink",
            category=drinks_category,
            price="30.00",
            description=(
                "Chilled carbonated soft drink."
            ),
            item_type=MenuItem.ItemType.NORMAL,
        )


        # -----------------------------------------
        # REALISTIC SET MENUS
        # -----------------------------------------

        set_menu_1 = save_food(
            name="Set Menu 1",
            category=set_menu_category,
            price="220.00",
            description=(
                "Chicken Fried Rice, Fried Chicken "
                "and Chinese Vegetable."
            ),
            item_type=MenuItem.ItemType.SET_MENU,
        )


        set_menu_2 = save_food(
            name="Set Menu 2",
            category=set_menu_category,
            price="260.00",
            description=(
                "Chicken Fried Rice, Fried Chicken, "
                "Chinese Vegetable and Soft Drink."
            ),
            item_type=MenuItem.ItemType.SET_MENU,
        )


        set_menu_3 = save_food(
            name="Set Menu 3",
            category=set_menu_category,
            price="320.00",
            description=(
                "Chicken Fried Rice, 2 pcs Fried Chicken, "
                "Chinese Vegetable and Soft Drink."
            ),
            item_type=MenuItem.ItemType.SET_MENU,
        )


        set_menu_4 = save_food(
            name="Set Menu 4",
            category=set_menu_category,
            price="380.00",
            description=(
                "Chicken Fried Rice, Fried Chicken, "
                "Chinese Vegetable, Chicken Chili Onion "
                "and Soft Drink."
            ),
            item_type=MenuItem.ItemType.SET_MENU,
        )


        set_menu_5 = save_food(
            name="Set Menu 5",
            category=set_menu_category,
            price="450.00",
            description=(
                "Chicken Fried Rice, Fried Chicken, "
                "Chinese Vegetable, Chicken Chili Onion, "
                "Thai Soup and Soft Drink."
            ),
            item_type=MenuItem.ItemType.SET_MENU,
        )


        # -----------------------------------------
        # SET MENU COMPONENT HELPER
        # -----------------------------------------

        def add_component(
            set_menu,
            component,
            quantity,
            display_order,
        ):

            obj, created = (
                SetMenuComponent.objects.update_or_create(
                    set_menu=set_menu,
                    component=component,
                    defaults={
                        "quantity": Decimal(
                            str(quantity)
                        ),
                        "display_order": display_order,
                    },
                )
            )

            return obj


        # -----------------------------------------
        # SET MENU 1
        # -----------------------------------------

        add_component(
            set_menu_1,
            chicken_fried_rice,
            1,
            1,
        )

        add_component(
            set_menu_1,
            fried_chicken,
            1,
            2,
        )

        add_component(
            set_menu_1,
            chinese_vegetable,
            1,
            3,
        )


        # -----------------------------------------
        # SET MENU 2
        # -----------------------------------------

        add_component(
            set_menu_2,
            chicken_fried_rice,
            1,
            1,
        )

        add_component(
            set_menu_2,
            fried_chicken,
            1,
            2,
        )

        add_component(
            set_menu_2,
            chinese_vegetable,
            1,
            3,
        )

        add_component(
            set_menu_2,
            soft_drink,
            1,
            4,
        )


        # -----------------------------------------
        # SET MENU 3
        # -----------------------------------------

        add_component(
            set_menu_3,
            chicken_fried_rice,
            1,
            1,
        )

        add_component(
            set_menu_3,
            fried_chicken,
            2,
            2,
        )

        add_component(
            set_menu_3,
            chinese_vegetable,
            1,
            3,
        )

        add_component(
            set_menu_3,
            soft_drink,
            1,
            4,
        )


        # -----------------------------------------
        # SET MENU 4
        # -----------------------------------------

        add_component(
            set_menu_4,
            chicken_fried_rice,
            1,
            1,
        )

        add_component(
            set_menu_4,
            fried_chicken,
            1,
            2,
        )

        add_component(
            set_menu_4,
            chinese_vegetable,
            1,
            3,
        )

        add_component(
            set_menu_4,
            chicken_chili_onion,
            1,
            4,
        )

        add_component(
            set_menu_4,
            soft_drink,
            1,
            5,
        )


        # -----------------------------------------
        # SET MENU 5
        # -----------------------------------------

        add_component(
            set_menu_5,
            chicken_fried_rice,
            1,
            1,
        )

        add_component(
            set_menu_5,
            fried_chicken,
            1,
            2,
        )

        add_component(
            set_menu_5,
            chinese_vegetable,
            1,
            3,
        )

        add_component(
            set_menu_5,
            chicken_chili_onion,
            1,
            4,
        )

        add_component(
            set_menu_5,
            thai_soup,
            1,
            5,
        )

        add_component(
            set_menu_5,
            soft_drink,
            1,
            6,
        )


        # -----------------------------------------
        # RESULT
        # -----------------------------------------

        self.stdout.write(
            self.style.SUCCESS(
                "Real menu successfully created."
            )
        )

        self.stdout.write(
            f"Restaurant: {restaurant.name}"
        )

        self.stdout.write(
            f"Old demo set menus archived: {archived_count}"
        )

        self.stdout.write(
            "Set Menu 1-5 created/updated."
        )

        self.stdout.write(
            "Chinese component foods created/updated."
        )

        self.stdout.write(
            f"Available menu items: "
            f"{MenuItem.objects.filter(category__restaurant=restaurant, is_available=True).count()}"
        )

        self.stdout.write(
            f"Set menu components: "
            f"{SetMenuComponent.objects.filter(set_menu__category__restaurant=restaurant).count()}"
        )