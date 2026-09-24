from decimal import Decimal
import json

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from inventory.models import Ingredient, IngredientCategory, Recipe, RecipeIngredient
from menu.addon_services import normalize_addon_ids, validate_item_addons
from menu.models import AddonGroup, AddonOption, Category, MenuItem
from orders.models import Order, OrderItem, OrderItemAddon
from restaurant.models import Floor, Restaurant, Table

User = get_user_model()


class AddonModelsAndServiceTests(TestCase):
    def setUp(self):
        self.restaurant = Restaurant.objects.create(name="Addon Bistro", address="Dhaka")
        self.category = Category.objects.create(restaurant=self.restaurant, name="Burgers")
        self.burger = MenuItem.objects.create(
            category=self.category,
            name="Classic Beef Burger",
            price=Decimal("350.00"),
            is_available=True,
        )

        # Single selection required group: Cheese Type
        self.cheese_group = AddonGroup.objects.create(
            restaurant=self.restaurant,
            name="Cheese Choice",
            selection_type=AddonGroup.SelectionType.SINGLE,
            is_required=True,
            min_selection=1,
            max_selection=1,
        )
        self.cheddar = AddonOption.objects.create(
            group=self.cheese_group,
            name="Cheddar",
            price=Decimal("40.00"),
            is_active=True,
        )
        self.swiss = AddonOption.objects.create(
            group=self.cheese_group,
            name="Swiss Cheese",
            price=Decimal("50.00"),
            is_active=True,
        )

        # Multiple selection optional group: Extra Toppings
        self.toppings_group = AddonGroup.objects.create(
            restaurant=self.restaurant,
            name="Extra Toppings",
            selection_type=AddonGroup.SelectionType.MULTIPLE,
            is_required=False,
            min_selection=0,
            max_selection=3,
        )
        self.bacon = AddonOption.objects.create(
            group=self.toppings_group,
            name="Beef Bacon",
            price=Decimal("60.00"),
            is_active=True,
        )
        self.mushrooms = AddonOption.objects.create(
            group=self.toppings_group,
            name="Sauteed Mushrooms",
            price=Decimal("45.00"),
            is_active=True,
        )
        self.jalapenos = AddonOption.objects.create(
            group=self.toppings_group,
            name="Pickled Jalapenos",
            price=Decimal("30.00"),
            is_active=False,  # Inactive option
        )

        # Assign groups to burger
        self.burger.addon_groups.set([self.cheese_group, self.toppings_group])

    def test_addon_group_clean_validation(self):
        # SINGLE group clean() automatically ensures max_selection is 1
        single_group = AddonGroup(
            restaurant=self.restaurant,
            name="Single Clean Test",
            selection_type=AddonGroup.SelectionType.SINGLE,
            is_required=True,
            max_selection=5,
        )
        single_group.clean()
        self.assertEqual(single_group.max_selection, 1)
        self.assertEqual(single_group.min_selection, 1)

        # MULTIPLE group with min > max should fail clean
        invalid_multiple = AddonGroup(
            restaurant=self.restaurant,
            name="Invalid Multi",
            selection_type=AddonGroup.SelectionType.MULTIPLE,
            min_selection=3,
            max_selection=1,
        )
        with self.assertRaises(ValidationError):
            invalid_multiple.clean()

        # MULTIPLE group with max_selection < 1 should fail clean
        invalid_max = AddonGroup(
            restaurant=self.restaurant,
            name="Invalid Max",
            selection_type=AddonGroup.SelectionType.MULTIPLE,
            max_selection=0,
        )
        with self.assertRaises(ValidationError):
            invalid_max.clean()

    def test_normalize_addon_ids(self):
        self.assertEqual(normalize_addon_ids([1, "2", 3]), [1, 2, 3])
        self.assertEqual(normalize_addon_ids("1, 2,  foo, 4"), [1, 2, 4])
        self.assertEqual(normalize_addon_ids(None), [])
        self.assertEqual(normalize_addon_ids(""), [])

    def test_validate_item_addons_valid_single_and_multiple(self):
        validated, total = validate_item_addons(self.burger, [self.cheddar.id, self.bacon.id])
        self.assertEqual(len(validated), 2)
        self.assertEqual(total, Decimal("100.00"))

    def test_validate_item_addons_missing_required(self):
        # Missing cheese choice (required single)
        with self.assertRaises(ValidationError) as ctx:
            validate_item_addons(self.burger, [self.bacon.id])
        self.assertIn("Please select an option for 'Cheese Choice'", str(ctx.exception))

    def test_validate_item_addons_multiple_single_selections(self):
        # Selecting both cheddar and swiss for single-choice group
        with self.assertRaises(ValidationError) as ctx:
            validate_item_addons(self.burger, [self.cheddar.id, self.swiss.id])
        self.assertIn("at most one option", str(ctx.exception))

    def test_validate_item_addons_inactive_option_rejected(self):
        # Jalapenos is inactive
        with self.assertRaises(ValidationError):
            validate_item_addons(self.burger, [self.cheddar.id, self.jalapenos.id])

    def test_validate_item_addons_foreign_restaurant_rejected(self):
        other_restaurant = Restaurant.objects.create(name="Other Rest", address="Sylhet")
        other_group = AddonGroup.objects.create(
            restaurant=other_restaurant,
            name="Foreign Group",
            selection_type=AddonGroup.SelectionType.MULTIPLE,
        )
        foreign_opt = AddonOption.objects.create(
            group=other_group,
            name="Foreign Sauce",
            price=Decimal("20.00"),
        )
        with self.assertRaises(ValidationError):
            validate_item_addons(self.burger, [self.cheddar.id, foreign_opt.id])

    def test_order_item_addon_snapshot_and_subtotal_backward_compatibility(self):
        order = Order.objects.create(
            restaurant=self.restaurant,
            order_type="DINE_IN",
            status="NEW",
            subtotal=Decimal("450.00"),
            total_amount=Decimal("450.00"),
        )

        order_item = OrderItem.objects.create(
            order=order,
            menu_item=self.burger,
            quantity=1,
            price=self.burger.price,  # 350.00
        )

        # Before addons: subtotal is just base price * qty
        self.assertEqual(order_item.addons_total, Decimal("0.00"))
        self.assertEqual(order_item.unit_price_with_addons, Decimal("350.00"))
        self.assertEqual(order_item.subtotal, Decimal("350.00"))

        # Add addon snapshots
        OrderItemAddon.objects.create(
            order_item=order_item,
            addon_option=self.cheddar,
            addon_group_name="Cheese Choice",
            addon_name="Cheddar",
            price=Decimal("40.00"),
        )
        OrderItemAddon.objects.create(
            order_item=order_item,
            addon_option=self.bacon,
            addon_group_name="Extra Toppings",
            addon_name="Beef Bacon",
            price=Decimal("60.00"),
        )

        # With addons: unit price is 350 + 40 + 60 = 450
        order_item.refresh_from_db()
        self.assertEqual(order_item.addons_total, Decimal("100.00"))
        self.assertEqual(order_item.unit_price_with_addons, Decimal("450.00"))
        self.assertEqual(order_item.subtotal, Decimal("450.00"))

        # Immutability check: if cheddar option price is changed in future, snapshot remains 40.00
        self.cheddar.price = Decimal("99.00")
        self.cheddar.save()

        order_item.refresh_from_db()
        self.assertEqual(order_item.addons_total, Decimal("100.00"))
        self.assertEqual(order_item.subtotal, Decimal("450.00"))


class AddonBackOfficeViewsTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.restaurant = Restaurant.objects.create(name="Bistro Owner", address="Chittagong")
        self.owner = User.objects.create_user(
            username="owner_user",
            email="owner@bistro.com",
            password="securepassword123",
            role="owner",
            restaurant=self.restaurant,
        )
        self.client.login(username="owner_user", password="securepassword123")
        self.category = Category.objects.create(restaurant=self.restaurant, name="Pizzas")
        self.pizza = MenuItem.objects.create(
            category=self.category,
            name="Margherita",
            price=Decimal("600.00"),
        )

    def test_addon_group_create_and_list(self):
        url = reverse("addon_group_create")
        post_data = {
            "name": "Crust Options",
            "selection_type": "SINGLE",
            "is_required": "1",
            "min_selection": "1",
            "max_selection": "1",
            "display_order": "0",
            "is_active": "1",
            "option_id[]": ["new_1", "new_2"],
            "option_name[]": ["Thin Crust", "Cheese Burst"],
            "option_price[]": ["0.00", "120.00"],
            "option_active[]": ["1", "1"],
            "menu_items": [str(self.pizza.id)],
        }
        response = self.client.post(url, post_data, follow=True)
        self.assertEqual(response.status_code, 200)

        group = AddonGroup.objects.get(name="Crust Options", restaurant=self.restaurant)
        self.assertEqual(group.options.count(), 2)
        self.assertEqual(group.menu_items.first(), self.pizza)

        list_url = reverse("addon_group_list")
        list_response = self.client.get(list_url)
        self.assertEqual(list_response.status_code, 200)
        self.assertContains(list_response, "Crust Options")
        self.assertContains(list_response, "Cheese Burst")

    def test_addon_group_edit_and_delete(self):
        group = AddonGroup.objects.create(
            restaurant=self.restaurant,
            name="Drink Size",
            selection_type=AddonGroup.SelectionType.SINGLE,
            is_required=True,
            min_selection=1,
            max_selection=1,
        )
        opt = AddonOption.objects.create(
            group=group,
            name="Regular",
            price=Decimal("0.00"),
        )
        edit_url = reverse("addon_group_edit", args=[group.id])
        post_data = {
            "name": "Beverage Size",
            "selection_type": "SINGLE",
            "is_required": "1",
            "min_selection": "1",
            "max_selection": "1",
            "display_order": "1",
            "is_active": "1",
            "option_id[]": [str(opt.id), "new_1"],
            "option_name[]": ["Regular 250ml", "Large 500ml"],
            "option_price[]": ["0.00", "40.00"],
            "option_active[]": ["1", "1"],
            "menu_items": [],
        }
        response = self.client.post(edit_url, post_data, follow=True)
        self.assertEqual(response.status_code, 200)

        group.refresh_from_db()
        self.assertEqual(group.name, "Beverage Size")
        self.assertEqual(group.options.count(), 2)

        # Delete
        del_url = reverse("addon_group_delete", args=[group.id])
        del_response = self.client.post(del_url, follow=True)
        self.assertEqual(del_response.status_code, 200)
        self.assertFalse(AddonGroup.objects.filter(id=group.id).exists())


class CustomerCartAndCheckoutAddonTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.restaurant = Restaurant.objects.create(name="QR Diner", address="Banani")
        self.floor = Floor.objects.create(restaurant=self.restaurant, floor_number=1, name="Ground")
        self.table = Table.objects.create(
            restaurant=self.restaurant,
            floor=self.floor,
            table_number=5,
            capacity=4,
            is_active=True,
        )
        self.category = Category.objects.create(restaurant=self.restaurant, name="Fast Food")
        self.burger = MenuItem.objects.create(
            category=self.category,
            name="Smash Burger",
            price=Decimal("250.00"),
            is_available=True,
        )

        self.sauce_group = AddonGroup.objects.create(
            restaurant=self.restaurant,
            name="Extra Sauce",
            selection_type=AddonGroup.SelectionType.MULTIPLE,
            is_required=False,
            min_selection=0,
            max_selection=2,
        )
        self.garlic_mayo = AddonOption.objects.create(
            group=self.sauce_group,
            name="Garlic Mayo",
            price=Decimal("25.00"),
        )
        self.spicy_bbq = AddonOption.objects.create(
            group=self.sauce_group,
            name="Spicy BBQ",
            price=Decimal("30.00"),
        )
        self.burger.addon_groups.add(self.sauce_group)

        self.ing_cat = IngredientCategory.objects.create(
            restaurant=self.restaurant, name="Meat"
        )
        self.beef = Ingredient.objects.create(
            restaurant=self.restaurant,
            category=self.ing_cat,
            name="Beef Patty",
            sku="BEEF-001",
            base_unit=Ingredient.BaseUnit.PIECE,
            pack_size=Decimal("1"),
            current_stock=Decimal("100"),
        )
        self.burger_recipe = Recipe.objects.create(menu_item=self.burger, yield_quantity=1)
        RecipeIngredient.objects.create(
            recipe=self.burger_recipe,
            ingredient=self.beef,
            quantity=Decimal("1"),
        )

    def test_customer_add_to_cart_and_checkout_with_addons(self):
        add_url = reverse("add_to_cart", args=[self.restaurant.id, self.table.id, self.burger.id])
        # Add to cart with Garlic Mayo (+25) and Spicy BBQ (+30)
        post_data = {
            "addons": [str(self.garlic_mayo.id), str(self.spicy_bbq.id)],
            "return_to": "cart",
        }
        res = self.client.post(add_url, post_data, follow=True)
        self.assertEqual(res.status_code, 200)

        # Cart should have composite key with unit price 250 + 55 = 305
        cart = self.client.session.get(f"cart_{self.restaurant.id}_{self.table.id}")
        self.assertIsNotNone(cart)
        expected_key = f"{self.burger.id}:{self.garlic_mayo.id},{self.spicy_bbq.id}"
        self.assertIn(expected_key, cart)
        self.assertEqual(cart[expected_key]["unit_price"], 305.0)

        # Now checkout
        checkout_url = reverse("checkout", args=[self.restaurant.id, self.table.id])
        checkout_post = {
            "order_type": "DINE_IN",
            "payment_timing": "PAY_LATER",
        }
        checkout_res = self.client.post(checkout_url, checkout_post, follow=True)
        self.assertEqual(checkout_res.status_code, 200)

        # Verify order and item addons
        order = Order.objects.filter(restaurant=self.restaurant).latest("id")
        self.assertEqual(order.subtotal, Decimal("305.00"))
        self.assertEqual(order.total_amount, Decimal("305.00"))

        item = order.items.first()
        self.assertEqual(item.price, Decimal("250.00"))
        self.assertEqual(item.addons.count(), 2)
        addon_names = set(item.addons.values_list("addon_name", flat=True))
        self.assertEqual(addon_names, {"Garlic Mayo", "Spicy BBQ"})


class PosAddonOrderTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.restaurant = Restaurant.objects.create(name="POS Diner", address="Gulshan")
        self.cashier = User.objects.create_user(
            username="cashier_user",
            email="cashier@pos.com",
            password="securepassword123",
            role="manager",
            restaurant=self.restaurant,
        )
        self.client.login(username="cashier_user", password="securepassword123")
        self.floor = Floor.objects.create(restaurant=self.restaurant, floor_number=1, name="Main")
        self.table = Table.objects.create(
            restaurant=self.restaurant,
            floor=self.floor,
            table_number=1,
            capacity=2,
            is_active=True,
        )
        self.category = Category.objects.create(restaurant=self.restaurant, name="Drinks")
        self.coffee = MenuItem.objects.create(
            category=self.category,
            name="Latte",
            price=Decimal("180.00"),
            is_available=True,
        )
        self.milk_group = AddonGroup.objects.create(
            restaurant=self.restaurant,
            name="Milk Choice",
            selection_type=AddonGroup.SelectionType.SINGLE,
            is_required=True,
            min_selection=1,
            max_selection=1,
        )
        self.oat_milk = AddonOption.objects.create(
            group=self.milk_group,
            name="Oat Milk",
            price=Decimal("40.00"),
        )
        self.coffee.addon_groups.add(self.milk_group)

        self.pos_ing_cat = IngredientCategory.objects.create(
            restaurant=self.restaurant, name="Beverages"
        )
        self.coffee_beans = Ingredient.objects.create(
            restaurant=self.restaurant,
            category=self.pos_ing_cat,
            name="Coffee Beans",
            sku="COFFEE-001",
            base_unit=Ingredient.BaseUnit.GRAM,
            pack_size=Decimal("1000"),
            current_stock=Decimal("5000"),
        )
        self.coffee_recipe = Recipe.objects.create(menu_item=self.coffee, yield_quantity=1)
        RecipeIngredient.objects.create(
            recipe=self.coffee_recipe,
            ingredient=self.coffee_beans,
            quantity=Decimal("18"),
        )

    def test_create_pos_order_with_valid_addons(self):
        url = reverse("create_pos_order")
        payload = {
            "order_type": "DINE_IN",
            "table_id": self.table.id,
            "items": [
                {
                    "id": self.coffee.id,
                    "quantity": 2,
                    "addon_ids": [self.oat_milk.id],
                }
            ],
            "discount_amount": 0,
            "service_percent": 0,
            "vat_percent": 0,
            "payment_timing": "PAY_LATER",
        }
        response = self.client.post(url, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])

        # Unit price = 180 + 40 = 220. Qty = 2 -> total 440
        order = Order.objects.get(id=data["order_id"])
        self.assertEqual(order.subtotal, Decimal("440.00"))
        self.assertEqual(order.total_amount, Decimal("440.00"))

        item = order.items.first()
        self.assertEqual(item.addons.count(), 1)
        addon_record = item.addons.first()
        self.assertEqual(addon_record.addon_name, "Oat Milk")
        self.assertEqual(addon_record.price, Decimal("40.00"))

    def test_create_pos_order_omitting_required_addon_fails(self):
        url = reverse("create_pos_order")
        payload = {
            "order_type": "DINE_IN",
            "table_id": self.table.id,
            "items": [
                {
                    "id": self.coffee.id,
                    "quantity": 1,
                    "addon_ids": [],  # Required milk choice omitted
                }
            ],
            "discount_amount": 0,
            "service_percent": 0,
            "vat_percent": 0,
            "payment_timing": "PAY_LATER",
        }
        response = self.client.post(url, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data["success"])
        self.assertIn("Milk Choice", data["message"])
