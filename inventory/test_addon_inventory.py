"""Tests for Addon Inventory Integration in Restaurant 360."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from finance.reports_services import get_profit_and_loss_data
from inventory.models import (
    AddonOptionIngredient,
    Ingredient,
    IngredientCategory,
    Recipe,
    RecipeIngredient,
    StockReservation,
    StockTransaction,
)
from inventory.services import (
    calculate_addon_available_portions,
    consume_order_reservations,
    get_order_requirements,
    release_order_reservations,
    reserve_stock_for_order,
)
from menu.addon_services import validate_item_addons
from menu.models import AddonGroup, AddonOption, Category, MenuItem, SetMenuComponent
from orders.models import Order, OrderItem, OrderItemAddon
from orders.services import archive_stale_order, transition_order_status
from restaurant.models import Restaurant, Table
from users.models import User


class AddonInventoryBaseTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.restaurant = Restaurant.objects.create(name="Addon Test Bistro", address="Banani, Dhaka")
        cls.other_restaurant = Restaurant.objects.create(name="Competitor Bistro", address="Gulshan, Dhaka")

        cls.table = Table.objects.create(restaurant=cls.restaurant, table_number=10)
        cls.category = Category.objects.create(restaurant=cls.restaurant, name="Burgers")

        cls.ing_cat = IngredientCategory.objects.create(restaurant=cls.restaurant, name="Pantry")
        cls.other_ing_cat = IngredientCategory.objects.create(restaurant=cls.other_restaurant, name="Other Pantry")

        # Ingredients for cls.restaurant
        cls.beef_patty = Ingredient.objects.create(
            restaurant=cls.restaurant,
            category=cls.ing_cat,
            name="Beef Patty",
            sku="PATTY-001",
            base_unit=Ingredient.BaseUnit.PIECE,
            pack_size=10,
            current_pack_price=Decimal("500.00"),  # 50.00 per PCS
            current_stock=Decimal("20.000"),
        )
        cls.cheese = Ingredient.objects.create(
            restaurant=cls.restaurant,
            category=cls.ing_cat,
            name="Cheddar Cheese",
            sku="CHEESE-001",
            base_unit=Ingredient.BaseUnit.GRAM,
            pack_size=1000,
            current_pack_price=Decimal("1200.00"),  # 1.20 per G
            current_stock=Decimal("500.000"),
        )
        cls.bun = Ingredient.objects.create(
            restaurant=cls.restaurant,
            category=cls.ing_cat,
            name="Burger Bun",
            sku="BUN-001",
            base_unit=Ingredient.BaseUnit.PIECE,
            pack_size=10,
            current_pack_price=Decimal("200.00"),  # 20.00 per PCS
            current_stock=Decimal("30.000"),
        )

        # Foreign ingredient for cls.other_restaurant
        cls.foreign_cheese = Ingredient.objects.create(
            restaurant=cls.other_restaurant,
            category=cls.other_ing_cat,
            name="Foreign Cheese",
            sku="F-CHEESE-001",
            base_unit=Ingredient.BaseUnit.GRAM,
            pack_size=1000,
            current_pack_price=Decimal("1000.00"),
            current_stock=Decimal("200.000"),
        )

        # Base Menu Item: Beef Burger (1 Bun, 1 Patty, 10g Cheese)
        cls.burger = MenuItem.objects.create(
            category=cls.category,
            name="Classic Beef Burger",
            price=Decimal("250.00"),
        )
        cls.burger_recipe = Recipe.objects.create(menu_item=cls.burger, yield_quantity=1)
        RecipeIngredient.objects.create(recipe=cls.burger_recipe, ingredient=cls.bun, quantity=Decimal("1.000"))
        RecipeIngredient.objects.create(recipe=cls.burger_recipe, ingredient=cls.beef_patty, quantity=Decimal("1.000"))
        RecipeIngredient.objects.create(recipe=cls.burger_recipe, ingredient=cls.cheese, quantity=Decimal("10.000"))

        # Addon Group: Extra Toppings
        cls.toppings_group = AddonGroup.objects.create(
            restaurant=cls.restaurant,
            name="Extra Toppings",
            selection_type=AddonGroup.SelectionType.MULTIPLE,
            is_required=False,
        )
        cls.burger.addon_groups.add(cls.toppings_group)

        # Addon Option 1: Extra Cheese (+৳40, requires 20g Cheddar Cheese)
        cls.opt_extra_cheese = AddonOption.objects.create(
            group=cls.toppings_group,
            name="Extra Cheese",
            price=Decimal("40.00"),
        )
        AddonOptionIngredient.objects.create(
            addon_option=cls.opt_extra_cheese,
            ingredient=cls.cheese,
            quantity=Decimal("20.000"),
        )

        # Addon Option 2: Extra Patty (+৳100, requires 1 Beef Patty)
        cls.opt_extra_patty = AddonOption.objects.create(
            group=cls.toppings_group,
            name="Extra Patty",
            price=Decimal("100.00"),
        )
        AddonOptionIngredient.objects.create(
            addon_option=cls.opt_extra_patty,
            ingredient=cls.beef_patty,
            quantity=Decimal("1.000"),
        )

        # Addon Option 3: Extra Mayo / Spicy (+৳10, no ingredient mapped)
        cls.opt_spicy_flavor = AddonOption.objects.create(
            group=cls.toppings_group,
            name="Spicy Seasoning",
            price=Decimal("10.00"),
        )

        # Manager user for management view tests
        cls.manager = User.objects.create_user(
            username="manager_addon_test",
            password="password123",
            role="owner",
            restaurant=cls.restaurant,
        )


class AddonOptionIngredientModelTests(AddonInventoryBaseTestCase):
    def test_addon_option_can_have_zero_or_multiple_ingredient_requirements(self):
        # Zero requirements
        self.assertEqual(self.opt_spicy_flavor.ingredient_requirements.count(), 0)

        # Single requirement
        self.assertEqual(self.opt_extra_cheese.ingredient_requirements.count(), 1)
        req = self.opt_extra_cheese.ingredient_requirements.first()
        self.assertEqual(req.ingredient, self.cheese)
        self.assertEqual(req.quantity, Decimal("20.000"))
        # Estimated cost = 20g * 1.20 = 24.00
        self.assertEqual(req.estimated_cost, Decimal("24.00000"))

        # Multiple requirements: create an option with both cheese and patty
        opt_combo = AddonOption.objects.create(
            group=self.toppings_group,
            name="Cheese & Patty Combo",
            price=Decimal("130.00"),
        )
        AddonOptionIngredient.objects.create(addon_option=opt_combo, ingredient=self.cheese, quantity=Decimal("15.000"))
        AddonOptionIngredient.objects.create(addon_option=opt_combo, ingredient=self.beef_patty, quantity=Decimal("1.000"))
        self.assertEqual(opt_combo.ingredient_requirements.count(), 2)

    def test_unique_ingredient_per_addon_option(self):
        with self.assertRaises((ValidationError, IntegrityError)):
            AddonOptionIngredient.objects.create(
                addon_option=self.opt_extra_cheese,
                ingredient=self.cheese,
                quantity=Decimal("10.000"),
            )

    def test_restaurant_isolation_enforced_on_clean(self):
        cross_req = AddonOptionIngredient(
            addon_option=self.opt_extra_cheese,
            ingredient=self.foreign_cheese,
            quantity=Decimal("10.000"),
        )
        with self.assertRaises(ValidationError) as ctx:
            cross_req.clean()
        self.assertIn("belong to the same restaurant", str(ctx.exception))


class OrderRequirementsWithAddonsTests(AddonInventoryBaseTestCase):
    def test_get_order_requirements_combines_base_recipe_and_addon_ingredients(self):
        order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_type="DINE_IN",
            status="NEW",
            total_amount=Decimal("290.00"),
        )
        item = OrderItem.objects.create(
            order=order,
            menu_item=self.burger,
            quantity=1,
            price=Decimal("250.00"),
        )
        OrderItemAddon.objects.create(
            order_item=item,
            addon_option=self.opt_extra_cheese,
            addon_group_name=self.toppings_group.name,
            addon_name=self.opt_extra_cheese.name,
            price=self.opt_extra_cheese.price,
        )

        requirements = get_order_requirements(order)
        # Bun: 1 PCS
        self.assertEqual(requirements[self.bun.pk]["required_quantity"], Decimal("1.000"))
        # Patty: 1 PCS
        self.assertEqual(requirements[self.beef_patty.pk]["required_quantity"], Decimal("1.000"))
        # Cheese: Base (10g) + Addon (20g) = 30g!
        self.assertEqual(requirements[self.cheese.pk]["required_quantity"], Decimal("30.000"))

    def test_order_with_multiple_quantity_and_multiple_addons(self):
        order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_type="DINE_IN",
            status="NEW",
            total_amount=Decimal("780.00"),
        )
        item = OrderItem.objects.create(
            order=order,
            menu_item=self.burger,
            quantity=2,
            price=Decimal("250.00"),
        )
        # 2 burgers, both with Extra Cheese (20g) and Extra Patty (1 PCS) and Spicy Seasoning (0g)
        OrderItemAddon.objects.create(
            order_item=item,
            addon_option=self.opt_extra_cheese,
            addon_name=self.opt_extra_cheese.name,
            price=self.opt_extra_cheese.price,
        )
        OrderItemAddon.objects.create(
            order_item=item,
            addon_option=self.opt_extra_patty,
            addon_name=self.opt_extra_patty.name,
            price=self.opt_extra_patty.price,
        )
        OrderItemAddon.objects.create(
            order_item=item,
            addon_option=self.opt_spicy_flavor,
            addon_name=self.opt_spicy_flavor.name,
            price=self.opt_spicy_flavor.price,
        )

        requirements = get_order_requirements(order)
        # Bun: 2 burgers * 1 = 2 PCS
        self.assertEqual(requirements[self.bun.pk]["required_quantity"], Decimal("2.000"))
        # Patty: 2 burgers * (1 base + 1 addon) = 4 PCS
        self.assertEqual(requirements[self.beef_patty.pk]["required_quantity"], Decimal("4.000"))
        # Cheese: 2 burgers * (10g base + 20g addon) = 60g
        self.assertEqual(requirements[self.cheese.pk]["required_quantity"], Decimal("60.000"))

    def test_addons_without_ingredients_do_not_alter_requirements(self):
        order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_type="DINE_IN",
            status="NEW",
            total_amount=Decimal("260.00"),
        )
        item = OrderItem.objects.create(
            order=order,
            menu_item=self.burger,
            quantity=1,
            price=Decimal("250.00"),
        )
        OrderItemAddon.objects.create(
            order_item=item,
            addon_option=self.opt_spicy_flavor,
            addon_name=self.opt_spicy_flavor.name,
            price=self.opt_spicy_flavor.price,
        )

        requirements = get_order_requirements(order)
        self.assertEqual(requirements[self.bun.pk]["required_quantity"], Decimal("1.000"))
        self.assertEqual(requirements[self.beef_patty.pk]["required_quantity"], Decimal("1.000"))
        self.assertEqual(requirements[self.cheese.pk]["required_quantity"], Decimal("10.000"))

    def test_set_menu_with_addons_combines_component_and_addon_requirements(self):
        # Create a set menu containing the burger
        set_item = MenuItem.objects.create(
            category=self.category,
            name="Burger Combo Deal",
            price=Decimal("320.00"),
            item_type=MenuItem.ItemType.SET_MENU,
        )
        SetMenuComponent.objects.create(set_menu=set_item, component=self.burger, quantity=Decimal("1.00"))
        set_item.addon_groups.add(self.toppings_group)

        order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_type="DINE_IN",
            status="NEW",
            total_amount=Decimal("360.00"),
        )
        item = OrderItem.objects.create(
            order=order,
            menu_item=set_item,
            quantity=1,
            price=Decimal("320.00"),
        )
        OrderItemAddon.objects.create(
            order_item=item,
            addon_option=self.opt_extra_cheese,
            addon_name=self.opt_extra_cheese.name,
            price=self.opt_extra_cheese.price,
        )

        requirements = get_order_requirements(order)
        # Component burger: 1 bun, 1 patty, 10g cheese. Addon: +20g cheese -> 30g cheese
        self.assertEqual(requirements[self.cheese.pk]["required_quantity"], Decimal("30.000"))
        self.assertEqual(requirements[self.beef_patty.pk]["required_quantity"], Decimal("1.000"))
        self.assertEqual(requirements[self.bun.pk]["required_quantity"], Decimal("1.000"))


class ReservationAndConsumptionLifecycleTests(AddonInventoryBaseTestCase):
    def test_new_order_reserves_base_recipe_and_addon_ingredients(self):
        order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_type="DINE_IN",
            status="NEW",
            total_amount=Decimal("390.00"),
        )
        item = OrderItem.objects.create(
            order=order,
            menu_item=self.burger,
            quantity=1,
            price=Decimal("250.00"),
        )
        OrderItemAddon.objects.create(
            order_item=item,
            addon_option=self.opt_extra_cheese,
            addon_name=self.opt_extra_cheese.name,
            price=self.opt_extra_cheese.price,
        )
        OrderItemAddon.objects.create(
            order_item=item,
            addon_option=self.opt_extra_patty,
            addon_name=self.opt_extra_patty.name,
            price=self.opt_extra_patty.price,
        )

        reservations = reserve_stock_for_order(order)
        self.assertEqual(len(reservations), 3)

        res_map = {r.ingredient_id: r for r in reservations}
        self.assertEqual(res_map[self.cheese.pk].quantity, Decimal("30.000"))
        self.assertEqual(res_map[self.beef_patty.pk].quantity, Decimal("2.000"))
        self.assertEqual(res_map[self.bun.pk].quantity, Decimal("1.000"))

        # Physical stock unchanged
        self.cheese.refresh_from_db()
        self.assertEqual(self.cheese.current_stock, Decimal("500.000"))
        self.assertEqual(self.cheese.available_stock, Decimal("470.000"))

    def test_preparing_consumes_both_base_and_addons_exactly_once(self):
        order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_type="DINE_IN",
            status="NEW",
            total_amount=Decimal("390.00"),
        )
        item = OrderItem.objects.create(
            order=order,
            menu_item=self.burger,
            quantity=1,
            price=Decimal("250.00"),
        )
        OrderItemAddon.objects.create(
            order_item=item,
            addon_option=self.opt_extra_cheese,
            addon_name=self.opt_extra_cheese.name,
            price=self.opt_extra_cheese.price,
        )
        OrderItemAddon.objects.create(
            order_item=item,
            addon_option=self.opt_extra_patty,
            addon_name=self.opt_extra_patty.name,
            price=self.opt_extra_patty.price,
        )

        reserve_stock_for_order(order)
        transition_order_status(order, "ACCEPTED")
        transition_order_status(order, "PREPARING")

        # Physical stock deducted
        self.cheese.refresh_from_db()
        self.beef_patty.refresh_from_db()
        self.bun.refresh_from_db()

        self.assertEqual(self.cheese.current_stock, Decimal("470.000"))  # 500 - 30
        self.assertEqual(self.beef_patty.current_stock, Decimal("18.000"))  # 20 - 2
        self.assertEqual(self.bun.current_stock, Decimal("29.000"))  # 30 - 1

        # All reservations CONSUMED
        self.assertEqual(
            StockReservation.objects.filter(order=order, status=StockReservation.Status.CONSUMED).count(),
            3,
        )

        # Stock transactions created with accurate unit_cost_snapshot
        txs = StockTransaction.objects.filter(order=order, transaction_type=StockTransaction.TransactionType.CONSUMPTION)
        self.assertEqual(txs.count(), 3)

        tx_cheese = txs.get(ingredient=self.cheese)
        self.assertEqual(tx_cheese.quantity, Decimal("30.000"))
        self.assertEqual(tx_cheese.unit_cost_snapshot, Decimal("1.200000"))

        tx_patty = txs.get(ingredient=self.beef_patty)
        self.assertEqual(tx_patty.quantity, Decimal("2.000"))
        self.assertEqual(tx_patty.unit_cost_snapshot, Decimal("50.000000"))

        # Subsequent status transitions do not double-consume
        transition_order_status(order, "READY")
        transition_order_status(order, "SERVED")

        self.cheese.refresh_from_db()
        self.assertEqual(self.cheese.current_stock, Decimal("470.000"))
        self.assertEqual(txs.count(), 3)

    def test_cancellation_before_consumption_releases_both_reservations(self):
        order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_type="DINE_IN",
            status="NEW",
            total_amount=Decimal("390.00"),
        )
        item = OrderItem.objects.create(
            order=order,
            menu_item=self.burger,
            quantity=1,
            price=Decimal("250.00"),
        )
        OrderItemAddon.objects.create(
            order_item=item,
            addon_option=self.opt_extra_cheese,
            addon_name=self.opt_extra_cheese.name,
            price=self.opt_extra_cheese.price,
        )

        reserve_stock_for_order(order)
        self.assertEqual(
            StockReservation.objects.filter(order=order, status=StockReservation.Status.ACTIVE).count(),
            3,
        )

        archive_stale_order(order)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_CANCELLED)

        # Reservations released
        self.assertEqual(
            StockReservation.objects.filter(order=order, status=StockReservation.Status.RELEASED).count(),
            3,
        )

        # Physical stock was never deducted
        self.cheese.refresh_from_db()
        self.assertEqual(self.cheese.current_stock, Decimal("500.000"))
        self.assertEqual(self.cheese.available_stock, Decimal("500.000"))

    def test_historical_cogs_uses_unit_cost_snapshot_for_addon_consumption(self):
        order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_type="DINE_IN",
            status="NEW",
            total_amount=Decimal("390.00"),
        )
        item = OrderItem.objects.create(
            order=order,
            menu_item=self.burger,
            quantity=1,
            price=Decimal("250.00"),
        )
        OrderItemAddon.objects.create(
            order_item=item,
            addon_option=self.opt_extra_cheese,
            addon_name=self.opt_extra_cheese.name,
            price=self.opt_extra_cheese.price,
        )
        reserve_stock_for_order(order)
        transition_order_status(order, "ACCEPTED")
        transition_order_status(order, "PREPARING")

        # Now change the current pack price of cheese in the catalog
        self.cheese.current_pack_price = Decimal("2400.00")  # double the price!
        self.cheese.save()

        # Historical COGS should still reflect the original unit_cost_snapshot of 1.20
        txs = StockTransaction.objects.filter(order=order, transaction_type=StockTransaction.TransactionType.CONSUMPTION)
        cogs = sum((tx.quantity * tx.unit_cost_snapshot for tx in txs), Decimal("0.00")).quantize(Decimal("0.01"))
        # Cheese: 30g * 1.20 = 36.00
        # Bun: 1 * 20.00 = 20.00
        # Patty: 1 * 50.00 = 50.00
        # Total COGS: 36 + 20 + 50 = 106.00
        self.assertEqual(cogs, Decimal("106.00"))

        today = timezone.now().date()
        start_dt = timezone.now() - timezone.timedelta(days=1)
        end_dt = timezone.now() + timezone.timedelta(days=1)
        pnl = get_profit_and_loss_data(self.restaurant, today, today, start_dt, end_dt)
        self.assertEqual(pnl["cogs"], Decimal("106.00"))


class AddonStockAvailabilityTests(AddonInventoryBaseTestCase):
    def test_calculate_addon_available_portions(self):
        # Modifier without ingredients -> 9999
        self.assertEqual(calculate_addon_available_portions(self.opt_spicy_flavor), 9999)

        # Extra Cheese (20g): available stock 500g -> 500 // 20 = 25 portions
        self.assertEqual(calculate_addon_available_portions(self.opt_extra_cheese), 25)

        # Extra Patty (1 PCS): available stock 20 -> 20 // 1 = 20 portions
        self.assertEqual(calculate_addon_available_portions(self.opt_extra_patty), 20)

        # Drain patty stock to 0
        self.beef_patty.current_stock = Decimal("0.000")
        self.beef_patty.save()
        self.assertEqual(calculate_addon_available_portions(self.opt_extra_patty), 0)

        # Inactive ingredient -> 0 portions
        self.cheese.is_active = False
        self.cheese.save()
        self.assertEqual(calculate_addon_available_portions(self.opt_extra_cheese), 0)

    def test_validate_item_addons_rejects_out_of_stock_addons(self):
        # When in stock, validation succeeds
        opts, total = validate_item_addons(self.burger, [self.opt_extra_cheese.id])
        self.assertEqual(len(opts), 1)
        self.assertEqual(total, Decimal("40.00"))

        # Deplete cheese
        self.cheese.current_stock = Decimal("0.000")
        self.cheese.save()

        with self.assertRaises(ValidationError) as ctx:
            validate_item_addons(self.burger, [self.opt_extra_cheese.id])
        self.assertIn("out of stock", str(ctx.exception).lower())

    def test_reserve_stock_rejects_order_if_addon_stock_is_insufficient(self):
        # Set available patty stock to 1
        self.beef_patty.current_stock = Decimal("1.000")
        self.beef_patty.save()

        # Order needs 1 for burger + 1 for Extra Patty = 2 patties
        order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            order_type="DINE_IN",
            status="NEW",
            total_amount=Decimal("350.00"),
        )
        item = OrderItem.objects.create(
            order=order,
            menu_item=self.burger,
            quantity=1,
            price=Decimal("250.00"),
        )
        OrderItemAddon.objects.create(
            order_item=item,
            addon_option=self.opt_extra_patty,
            addon_name=self.opt_extra_patty.name,
            price=self.opt_extra_patty.price,
        )

        with self.assertRaises(ValidationError) as ctx:
            reserve_stock_for_order(order)
        self.assertIn("Insufficient stock", str(ctx.exception))


class AddonManagementViewTests(AddonInventoryBaseTestCase):
    def setUp(self):
        self.client.force_login(self.manager)

    def test_addon_group_create_with_ingredient_mapping(self):
        url = reverse("addon_group_create")
        post_data = {
            "name": "Burger Upgrades",
            "selection_type": "MULTIPLE",
            "is_required": "0",
            "min_selection": "0",
            "max_selection": "",
            "display_order": "0",
            "is_active": "1",
            "option_key[]": ["opt_key_1", "opt_key_2"],
            "option_name[]": ["Double Cheddar", "Plain Upgrade"],
            "option_price[]": ["50.00", "0.00"],
            "option_active[]": ["1", "1"],
            "option_ingredient_opt_key_1[]": [str(self.cheese.id)],
            "option_quantity_opt_key_1[]": ["25.000"],
            "menu_items": [str(self.burger.id)],
        }
        response = self.client.post(url, post_data, follow=True)
        self.assertEqual(response.status_code, 200)

        group = AddonGroup.objects.get(name="Burger Upgrades", restaurant=self.restaurant)
        self.assertEqual(group.options.count(), 2)

        opt_cheddar = group.options.get(name="Double Cheddar")
        self.assertEqual(opt_cheddar.ingredient_requirements.count(), 1)
        req = opt_cheddar.ingredient_requirements.first()
        self.assertEqual(req.ingredient, self.cheese)
        self.assertEqual(req.quantity, Decimal("25.000"))

        opt_plain = group.options.get(name="Plain Upgrade")
        self.assertEqual(opt_plain.ingredient_requirements.count(), 0)

    def test_addon_group_edit_updates_and_syncs_ingredient_mappings(self):
        group = AddonGroup.objects.create(
            restaurant=self.restaurant,
            name="Salad Dressings",
            selection_type=AddonGroup.SelectionType.SINGLE,
        )
        opt = AddonOption.objects.create(
            group=group,
            name="House Dressing",
            price=Decimal("20.00"),
        )
        AddonOptionIngredient.objects.create(
            addon_option=opt,
            ingredient=self.cheese,
            quantity=Decimal("5.000"),
        )

        edit_url = reverse("addon_group_edit", args=[group.id])
        # Update option to require Beef Patty instead of Cheese
        post_data = {
            "name": "Salad Dressings",
            "selection_type": "SINGLE",
            "is_required": "0",
            "min_selection": "0",
            "max_selection": "",
            "display_order": "0",
            "is_active": "1",
            "option_key[]": [f"opt_{opt.id}"],
            "option_id[]": [str(opt.id)],
            "option_name[]": ["House Dressing"],
            "option_price[]": ["20.00"],
            "option_active[]": ["1"],
            f"option_ingredient_opt_{opt.id}[]": [str(self.beef_patty.id)],
            f"option_quantity_opt_{opt.id}[]": ["1.000"],
            "menu_items": [],
        }
        response = self.client.post(edit_url, post_data, follow=True)
        self.assertEqual(response.status_code, 200)

        opt.refresh_from_db()
        self.assertEqual(opt.ingredient_requirements.count(), 1)
        req = opt.ingredient_requirements.first()
        self.assertEqual(req.ingredient, self.beef_patty)
        self.assertEqual(req.quantity, Decimal("1.000"))

    def test_addon_group_create_rejects_foreign_ingredient(self):
        url = reverse("addon_group_create")
        post_data = {
            "name": "Invalid Group",
            "selection_type": "SINGLE",
            "is_required": "0",
            "min_selection": "0",
            "max_selection": "",
            "display_order": "0",
            "is_active": "1",
            "option_key[]": ["opt_1"],
            "option_name[]": ["Foreign Addon"],
            "option_price[]": ["30.00"],
            "option_active[]": ["1"],
            "option_ingredient_opt_1[]": [str(self.foreign_cheese.id)],
            "option_quantity_opt_1[]": ["10.000"],
            "menu_items": [],
        }
        response = self.client.post(url, post_data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AddonGroup.objects.filter(name="Invalid Group").exists())
        self.assertContains(response, "active ingredient")
