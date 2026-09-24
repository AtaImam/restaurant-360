"""Branch isolation regression tests.

Verifies that POS, Kitchen, QR checkout, inventory reservation and all
operational queries are scoped to the active branch and do not leak data
across branches of the same restaurant.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from inventory.models import (
    BranchIngredientStock,
    Ingredient,
    IngredientCategory,
    Recipe,
    RecipeIngredient,
    StockReservation,
)
from menu.models import Category, MenuItem
from orders.models import Order, TableSession
from orders.services import orders_for_user
from restaurant.branch_services import get_active_branch
from restaurant.models import Branch, Floor, Restaurant, Table

User = get_user_model()


# ---------------------------------------------------------------------------
# Shared fixture factory
# ---------------------------------------------------------------------------

def _make_branch_fixture(restaurant, branch_name, *, is_main=False):
    """Create a branch with a floor, table, ingredient, and menu item with recipe."""
    # Generate a unique code to avoid UniqueConstraint collisions across test classes
    import hashlib
    unique_suffix = hashlib.md5(f"{restaurant.id}-{branch_name}".encode()).hexdigest()[:4].upper()
    code = (branch_name[:4].upper().replace(" ", "") + unique_suffix)[:20]
    branch = Branch.objects.create(
        restaurant=restaurant,
        name=branch_name,
        code=code,
        is_main=is_main,
    )
    floor = Floor.objects.create(
        restaurant=restaurant,
        branch=branch,
        name=f"Floor - {branch_name}",
        floor_number=1,
    )
    table = Table.objects.create(
        restaurant=restaurant,
        branch=branch,
        floor=floor,
        table_number=1,
        capacity=4,
        status=Table.STATUS_AVAILABLE,
        qr_code=f"test/{branch_name}_t1.png",
    )
    return branch, floor, table


def _make_shared_ingredient(restaurant, ing_cat):
    """Create a restaurant-level ingredient with BranchIngredientStock auto-created."""
    ing = Ingredient.objects.create(
        restaurant=restaurant,
        category=ing_cat,
        name=f"Flour-{Ingredient.objects.count()}",
        sku=f"FL-{Ingredient.objects.count():04d}",
        base_unit=Ingredient.BaseUnit.GRAM,
        pack_size=Decimal("1000"),
        current_stock=Decimal("5000.000"),
    )
    return ing


# ---------------------------------------------------------------------------
# 1. Branch model & auto-assignment
# ---------------------------------------------------------------------------

class BranchAutoAssignmentTests(TestCase):
    """Order.save() must resolve branch from table or fall back to main branch."""

    @classmethod
    def setUpTestData(cls):
        cls.restaurant = Restaurant.objects.create(name="Auto-Assign Diner", address="Dhaka")
        cls.main_branch, cls.main_floor, cls.main_table = _make_branch_fixture(
            cls.restaurant, "Main", is_main=True
        )
        cls.other_branch, cls.other_floor, cls.other_table = _make_branch_fixture(
            cls.restaurant, "City"
        )
        cls.category = Category.objects.create(name="Mains", restaurant=cls.restaurant)
        cls.item = MenuItem.objects.create(
            name="Steak", category=cls.category, price=Decimal("500.00"), is_available=True
        )

    def test_order_branch_resolved_from_table(self):
        order = Order.objects.create(
            restaurant=self.restaurant,
            table=self.other_table,
            order_type="DINE_IN",
            status="NEW",
            total_amount=Decimal("500.00"),
        )
        self.assertEqual(order.branch_id, self.other_branch.id)

    def test_order_without_table_gets_main_branch(self):
        order = Order.objects.create(
            restaurant=self.restaurant,
            order_type="TAKEAWAY",
            status="NEW",
            total_amount=Decimal("500.00"),
        )
        self.assertEqual(order.branch_id, self.main_branch.id)

    def test_table_session_inherits_branch_from_table(self):
        session = TableSession.objects.create(
            restaurant=self.restaurant,
            table=self.other_table,
        )
        self.assertEqual(session.branch_id, self.other_branch.id)


# ---------------------------------------------------------------------------
# 2. orders_for_user branch scoping
# ---------------------------------------------------------------------------

class OrdersForUserBranchTests(TestCase):
    """orders_for_user must scope by branch when the user has an assigned branch."""

    @classmethod
    def setUpTestData(cls):
        cls.restaurant = Restaurant.objects.create(name="Scoping Test Diner", address="Dhaka")
        cls.branch_a, _, cls.table_a = _make_branch_fixture(cls.restaurant, "Alpha", is_main=True)
        cls.branch_b, _, cls.table_b = _make_branch_fixture(cls.restaurant, "Beta")

        cls.staff_a = User.objects.create_user(
            username="staff_a", email="sa@test.com", password="pw",
            role="waiter", restaurant=cls.restaurant, branch=cls.branch_a,
        )
        cls.staff_b = User.objects.create_user(
            username="staff_b", email="sb@test.com", password="pw",
            role="waiter", restaurant=cls.restaurant, branch=cls.branch_b,
        )
        cls.owner = User.objects.create_user(
            username="owner_x", email="ox@test.com", password="pw",
            role="owner", restaurant=cls.restaurant,
        )

        cls.order_a = Order.objects.create(
            restaurant=cls.restaurant, branch=cls.branch_a,
            table=cls.table_a, order_type="DINE_IN",
            status="NEW", total_amount=Decimal("200.00"),
        )
        cls.order_b = Order.objects.create(
            restaurant=cls.restaurant, branch=cls.branch_b,
            table=cls.table_b, order_type="DINE_IN",
            status="NEW", total_amount=Decimal("300.00"),
        )

    def test_branch_staff_sees_only_own_branch_orders(self):
        qs = orders_for_user(self.staff_a)
        ids = set(qs.values_list("id", flat=True))
        self.assertIn(self.order_a.id, ids)
        self.assertNotIn(self.order_b.id, ids)

    def test_other_branch_staff_sees_only_own_branch(self):
        qs = orders_for_user(self.staff_b)
        ids = set(qs.values_list("id", flat=True))
        self.assertIn(self.order_b.id, ids)
        self.assertNotIn(self.order_a.id, ids)

    def test_owner_sees_all_restaurant_orders(self):
        qs = orders_for_user(self.owner)
        ids = set(qs.values_list("id", flat=True))
        self.assertIn(self.order_a.id, ids)
        self.assertIn(self.order_b.id, ids)


# ---------------------------------------------------------------------------
# 3. get_active_branch resolver
# ---------------------------------------------------------------------------

class GetActiveBranchTests(TestCase):
    """get_active_branch must resolve the correct branch based on user role/assignment."""

    @classmethod
    def setUpTestData(cls):
        cls.restaurant = Restaurant.objects.create(name="Branch Resolver Test", address="Dhaka")
        cls.main_branch = Branch.objects.create(
            restaurant=cls.restaurant, name="Main", code="MAIN", is_main=True,
        )
        cls.city_branch = Branch.objects.create(
            restaurant=cls.restaurant, name="City", code="CITY",
        )
        cls.owner = User.objects.create_user(
            username="owner_br", email="obr@test.com", password="pw",
            role="owner", restaurant=cls.restaurant,
        )
        cls.waiter = User.objects.create_user(
            username="waiter_br", email="wbr@test.com", password="pw",
            role="waiter", restaurant=cls.restaurant, branch=cls.city_branch,
        )

    def _request(self, user, session_data=None):
        from django.test import RequestFactory
        rf = RequestFactory()
        req = rf.get("/")
        req.user = user
        req.session = session_data or {}
        return req

    def test_staff_gets_assigned_branch(self):
        req = self._request(self.waiter)
        branch = get_active_branch(req, self.restaurant)
        self.assertEqual(branch.id, self.city_branch.id)

    def test_owner_defaults_to_main_branch(self):
        req = self._request(self.owner)
        branch = get_active_branch(req, self.restaurant)
        self.assertEqual(branch.id, self.main_branch.id)

    def test_owner_can_switch_branch_via_session(self):
        req = self._request(self.owner, session_data={"active_branch_id": self.city_branch.id})
        branch = get_active_branch(req, self.restaurant)
        self.assertEqual(branch.id, self.city_branch.id)


# ---------------------------------------------------------------------------
# 4. POS dashboard — cross-branch table leakage
# ---------------------------------------------------------------------------

class POSDashboardBranchIsolationTests(TestCase):
    """POS dashboard must only show tables from the active branch."""

    @classmethod
    def setUpTestData(cls):
        cls.restaurant = Restaurant.objects.create(name="POS Branch Test", address="Dhaka")
        cls.branch_a, _, cls.table_a = _make_branch_fixture(cls.restaurant, "North", is_main=True)
        cls.branch_b, _, cls.table_b = _make_branch_fixture(cls.restaurant, "South")

        cls.owner = User.objects.create_user(
            username="pos_owner", email="po@test.com", password="pw",
            role="owner", restaurant=cls.restaurant,
        )
        cls.waiter_a = User.objects.create_user(
            username="pos_waiter_a", email="pwa@test.com", password="pw",
            role="waiter", restaurant=cls.restaurant, branch=cls.branch_a,
        )
        cls.waiter_b = User.objects.create_user(
            username="pos_waiter_b", email="pwb@test.com", password="pw",
            role="waiter", restaurant=cls.restaurant, branch=cls.branch_b,
        )

    def _login_client(self, user):
        c = Client()
        c.force_login(user)
        return c

    def test_waiter_a_sees_only_branch_a_tables(self):
        client = self._login_client(self.waiter_a)
        resp = client.get(reverse("pos_dashboard"))
        self.assertEqual(resp.status_code, 200)
        table_ids = [t.id for t in resp.context["tables"]]
        self.assertIn(self.table_a.id, table_ids)
        self.assertNotIn(self.table_b.id, table_ids)

    def test_waiter_b_sees_only_branch_b_tables(self):
        client = self._login_client(self.waiter_b)
        resp = client.get(reverse("pos_dashboard"))
        self.assertEqual(resp.status_code, 200)
        table_ids = [t.id for t in resp.context["tables"]]
        self.assertIn(self.table_b.id, table_ids)
        self.assertNotIn(self.table_a.id, table_ids)

    def test_owner_active_north_branch_sees_north_table(self):
        client = self._login_client(self.owner)
        session = client.session
        session["active_branch_id"] = self.branch_a.id
        session.save()
        resp = client.get(reverse("pos_dashboard"))
        self.assertEqual(resp.status_code, 200)
        table_ids = [t.id for t in resp.context["tables"]]
        self.assertIn(self.table_a.id, table_ids)
        self.assertNotIn(self.table_b.id, table_ids)


# ---------------------------------------------------------------------------
# 5. BranchIngredientStock auto-creation
# ---------------------------------------------------------------------------

class BranchIngredientStockAutoCreationTests(TestCase):
    """BranchIngredientStock records must be created when ingredients or branches are created."""

    @classmethod
    def setUpTestData(cls):
        cls.restaurant = Restaurant.objects.create(name="Stock Auto-Create Test", address="Dhaka")
        cls.main_branch = Branch.objects.create(
            restaurant=cls.restaurant, name="Main", code="MAIN", is_main=True,
        )
        cls.ing_cat = IngredientCategory.objects.create(
            restaurant=cls.restaurant, name="Produce"
        )

    def test_new_ingredient_creates_stock_for_existing_branches(self):
        ing = Ingredient.objects.create(
            restaurant=self.restaurant,
            category=self.ing_cat,
            name="Tomato",
            sku="TOM-001",
            base_unit=Ingredient.BaseUnit.GRAM,
            pack_size=Decimal("1000"),
            current_stock=Decimal("500.000"),
        )
        stock = BranchIngredientStock.objects.filter(
            branch=self.main_branch, ingredient=ing
        ).first()
        self.assertIsNotNone(stock, "BranchIngredientStock should be auto-created for existing branches.")

    def test_new_branch_creates_stock_for_existing_ingredients(self):
        ing = Ingredient.objects.create(
            restaurant=self.restaurant,
            category=self.ing_cat,
            name="Onion",
            sku="ONI-002",
            base_unit=Ingredient.BaseUnit.GRAM,
            pack_size=Decimal("500"),
            current_stock=Decimal("300.000"),
        )
        new_branch = Branch.objects.create(
            restaurant=self.restaurant, name="Airport", code="AIRP",
        )
        stock = BranchIngredientStock.objects.filter(
            branch=new_branch, ingredient=ing
        ).first()
        self.assertIsNotNone(stock, "BranchIngredientStock should be auto-created for the new branch.")


# ---------------------------------------------------------------------------
# 6. Cross-branch POS order creation rejection
# ---------------------------------------------------------------------------

class POSCrossBranchOrderRejectionTests(TestCase):
    """POS create_pos_order must reject a table that does not belong to the active branch."""

    @classmethod
    def setUpTestData(cls):
        import json
        cls.restaurant = Restaurant.objects.create(name="Cross Branch POS Test", address="Dhaka")
        cls.branch_a, _, cls.table_a = _make_branch_fixture(cls.restaurant, "West", is_main=True)
        cls.branch_b, _, cls.table_b = _make_branch_fixture(cls.restaurant, "East")

        cls.category = Category.objects.create(name="Drinks", restaurant=cls.restaurant)
        cls.item = MenuItem.objects.create(
            name="Juice", category=cls.category, price=Decimal("80.00"), is_available=True
        )
        cls.waiter_a = User.objects.create_user(
            username="xb_waiter_a", email="xbwa@test.com", password="pw",
            role="waiter", restaurant=cls.restaurant, branch=cls.branch_a,
        )

    def test_waiter_a_cannot_create_order_on_branch_b_table(self):
        import json
        client = Client()
        client.force_login(self.waiter_a)
        payload = {
            "order_type": "DINE_IN",
            "table_id": self.table_b.id,
            "items": [{"id": self.item.id, "quantity": 1, "addon_ids": []}],
        }
        resp = client.post(
            reverse("create_pos_order"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        # Must be rejected (400/404), NOT silently succeed
        self.assertIn(resp.status_code, [400, 404])
        data = resp.json()
        self.assertFalse(data.get("success", True))


# ---------------------------------------------------------------------------
# 7. Stock reservation branch scoping
# ---------------------------------------------------------------------------

class StockReservationBranchTests(TestCase):
    """Stock reservations must carry the branch FK from the order."""

    @classmethod
    def setUpTestData(cls):
        cls.restaurant = Restaurant.objects.create(name="Reservation Branch Test", address="Dhaka")
        cls.branch_a, _, cls.table_a = _make_branch_fixture(cls.restaurant, "Reserve-A", is_main=True)

        cls.ing_cat = IngredientCategory.objects.create(
            restaurant=cls.restaurant, name="Baking"
        )
        cls.ing = Ingredient.objects.create(
            restaurant=cls.restaurant,
            category=cls.ing_cat,
            name="Sugar",
            sku="SUG-001",
            base_unit=Ingredient.BaseUnit.GRAM,
            pack_size=Decimal("1000"),
            current_stock=Decimal("5000.000"),
        )
        cls.category = Category.objects.create(name="Desserts", restaurant=cls.restaurant)
        cls.item = MenuItem.objects.create(
            name="Cake", category=cls.category, price=Decimal("200.00"), is_available=True
        )
        recipe = Recipe.objects.create(menu_item=cls.item, yield_quantity=1)
        RecipeIngredient.objects.create(recipe=recipe, ingredient=cls.ing, quantity=Decimal("100"))

    def test_reservation_carries_branch_from_order(self):
        from inventory.services import reserve_stock_for_order
        order = Order.objects.create(
            restaurant=self.restaurant,
            branch=self.branch_a,
            table=self.table_a,
            order_type="DINE_IN",
            status="NEW",
            total_amount=Decimal("200.00"),
        )
        from orders.models import OrderItem
        OrderItem.objects.create(
            order=order, menu_item=self.item, quantity=1, price=Decimal("200.00")
        )
        reservations = reserve_stock_for_order(order)
        self.assertTrue(len(reservations) > 0)
        for r in reservations:
            self.assertEqual(r.branch_id, self.branch_a.id,
                             "StockReservation must carry branch from the order.")


# ---------------------------------------------------------------------------
# 8. Kitchen dashboard branch isolation
# ---------------------------------------------------------------------------

class KitchenDashboardBranchTests(TestCase):
    """Kitchen dashboard must only return orders for the user's assigned branch."""

    @classmethod
    def setUpTestData(cls):
        cls.restaurant = Restaurant.objects.create(name="Kitchen Branch Test", address="Dhaka")
        cls.branch_a, _, cls.table_a = _make_branch_fixture(cls.restaurant, "Kitchen-A", is_main=True)
        cls.branch_b, _, cls.table_b = _make_branch_fixture(cls.restaurant, "Kitchen-B")

        cls.chef_a = User.objects.create_user(
            username="chef_a", email="ca@test.com", password="pw",
            role="chef", restaurant=cls.restaurant, branch=cls.branch_a,
        )
        cls.chef_b = User.objects.create_user(
            username="chef_b", email="cb@test.com", password="pw",
            role="chef", restaurant=cls.restaurant, branch=cls.branch_b,
        )

        cls.order_a = Order.objects.create(
            restaurant=cls.restaurant, branch=cls.branch_a,
            table=cls.table_a, order_type="DINE_IN",
            status="NEW", total_amount=Decimal("100.00"),
        )
        cls.order_b = Order.objects.create(
            restaurant=cls.restaurant, branch=cls.branch_b,
            table=cls.table_b, order_type="DINE_IN",
            status="NEW", total_amount=Decimal("150.00"),
        )

    def test_chef_a_kitchen_sees_only_branch_a_orders(self):
        client = Client()
        client.force_login(self.chef_a)
        resp = client.get(reverse("kitchen_dashboard"))
        self.assertEqual(resp.status_code, 200)
        order_ids = [o.id for o in resp.context["orders"]]
        self.assertIn(self.order_a.id, order_ids)
        self.assertNotIn(self.order_b.id, order_ids)

    def test_chef_b_kitchen_sees_only_branch_b_orders(self):
        client = Client()
        client.force_login(self.chef_b)
        resp = client.get(reverse("kitchen_dashboard"))
        self.assertEqual(resp.status_code, 200)
        order_ids = [o.id for o in resp.context["orders"]]
        self.assertIn(self.order_b.id, order_ids)
        self.assertNotIn(self.order_a.id, order_ids)
