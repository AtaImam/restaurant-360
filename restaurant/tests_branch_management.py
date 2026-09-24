"""Branch Management UI regression tests.

Covers:
- branch list/create/edit access control (owner vs staff)
- branch toggle (activate/deactivate) with Main Branch protection
- branch delete protection (Main Branch + order history)
- switch_branch session endpoint
- BranchForm validation (duplicate name/code)
- context_processor exposes active_branch / user_branches / can_switch_branch
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from orders.models import Order
from restaurant.forms import BranchForm
from restaurant.models import Branch, Floor, Restaurant, Table

User = get_user_model()


def _make_restaurant_owner(username, restaurant):
    return User.objects.create_user(
        username=username,
        email=f"{username}@test.com",
        password="pw",
        role="owner",
        restaurant=restaurant,
        is_active_staff=True,
    )


def _make_staff(username, restaurant, branch=None, role="waiter"):
    return User.objects.create_user(
        username=username,
        email=f"{username}@test.com",
        password="pw",
        role=role,
        restaurant=restaurant,
        branch=branch,
        is_active_staff=True,
    )


class BranchListAccessTests(TestCase):
    """Owner can access branch list; non-owner staff cannot."""

    @classmethod
    def setUpTestData(cls):
        cls.restaurant = Restaurant.objects.create(name="Access Test Diner", address="Dhaka")
        cls.owner = _make_restaurant_owner("access_owner", cls.restaurant)
        cls.waiter = _make_staff("access_waiter", cls.restaurant, role="waiter")

    def test_owner_can_access_branch_list(self):
        c = Client()
        c.force_login(self.owner)
        resp = c.get(reverse("restaurant:branch_list"))
        self.assertEqual(resp.status_code, 200)

    def test_staff_cannot_access_branch_list(self):
        c = Client()
        c.force_login(self.waiter)
        resp = c.get(reverse("restaurant:branch_list"))
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_redirects_to_login(self):
        c = Client()
        resp = c.get(reverse("restaurant:branch_list"))
        self.assertIn(resp.status_code, [302, 403])


class BranchCreateTests(TestCase):
    """Owner can create branches; duplicate names/codes are rejected."""

    @classmethod
    def setUpTestData(cls):
        cls.restaurant = Restaurant.objects.create(name="Create Test Diner", address="Dhaka")
        cls.owner = _make_restaurant_owner("create_owner", cls.restaurant)
        # Main branch already exists from restaurant creation (via backfill migration)
        # but in tests there's no such signal – just the branches we create
        Branch.objects.create(
            restaurant=cls.restaurant, name="Main", code="MAIN", is_main=True
        )

    def _client(self):
        c = Client()
        c.force_login(self.owner)
        return c

    def test_create_valid_branch(self):
        c = self._client()
        resp = c.post(
            reverse("restaurant:branch_create"),
            {"name": "Gulshan", "code": "GLN", "address": "Gulshan Ave", "phone": "", "email": ""},
        )
        self.assertRedirects(resp, reverse("restaurant:branch_list"))
        self.assertTrue(Branch.objects.filter(restaurant=self.restaurant, code="GLN").exists())

    def test_duplicate_name_rejected(self):
        c = self._client()
        resp = c.post(
            reverse("restaurant:branch_create"),
            {"name": "Main", "code": "MN2", "address": "", "phone": "", "email": ""},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "already exists")

    def test_duplicate_code_rejected(self):
        c = self._client()
        resp = c.post(
            reverse("restaurant:branch_create"),
            {"name": "Another Branch", "code": "MAIN", "address": "", "phone": "", "email": ""},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "already exists")

    def test_empty_name_rejected(self):
        c = self._client()
        resp = c.post(
            reverse("restaurant:branch_create"),
            {"name": "", "code": "XYZ", "address": "", "phone": "", "email": ""},
        )
        self.assertEqual(resp.status_code, 200)


class BranchEditTests(TestCase):
    """Owner can edit branch name/code; unique constraints still apply."""

    @classmethod
    def setUpTestData(cls):
        cls.restaurant = Restaurant.objects.create(name="Edit Test Diner", address="Dhaka")
        cls.owner = _make_restaurant_owner("edit_owner", cls.restaurant)
        cls.main_branch = Branch.objects.create(
            restaurant=cls.restaurant, name="Main", code="MAIN", is_main=True
        )
        cls.other_branch = Branch.objects.create(
            restaurant=cls.restaurant, name="City", code="CITY"
        )

    def _client(self):
        c = Client()
        c.force_login(self.owner)
        return c

    def test_edit_branch_name(self):
        c = self._client()
        resp = c.post(
            reverse("restaurant:branch_edit", args=[self.other_branch.id]),
            {"name": "Renamed City", "code": "CITY", "address": "", "phone": "", "email": ""},
        )
        self.assertRedirects(resp, reverse("restaurant:branch_list"))
        self.other_branch.refresh_from_db()
        self.assertEqual(self.other_branch.name, "Renamed City")

    def test_edit_cannot_use_existing_code(self):
        c = self._client()
        resp = c.post(
            reverse("restaurant:branch_edit", args=[self.other_branch.id]),
            {"name": "City", "code": "MAIN", "address": "", "phone": "", "email": ""},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "already exists")

    def test_staff_cannot_edit(self):
        waiter = _make_staff("edit_waiter", self.restaurant)
        c = Client()
        c.force_login(waiter)
        resp = c.get(reverse("restaurant:branch_edit", args=[self.other_branch.id]))
        self.assertEqual(resp.status_code, 403)


class BranchToggleTests(TestCase):
    """Main Branch cannot be deactivated. Non-main can toggle freely when no live orders."""

    @classmethod
    def setUpTestData(cls):
        cls.restaurant = Restaurant.objects.create(name="Toggle Test Diner", address="Dhaka")
        cls.owner = _make_restaurant_owner("toggle_owner", cls.restaurant)
        cls.main_branch = Branch.objects.create(
            restaurant=cls.restaurant, name="Main", code="MAIN", is_main=True
        )
        cls.city_branch = Branch.objects.create(
            restaurant=cls.restaurant, name="City", code="CTY"
        )

    def _client(self):
        c = Client()
        c.force_login(self.owner)
        return c

    def test_main_branch_cannot_be_deactivated(self):
        c = self._client()
        resp = c.post(reverse("restaurant:branch_toggle", args=[self.main_branch.id]))
        self.assertRedirects(resp, reverse("restaurant:branch_list"))
        self.main_branch.refresh_from_db()
        self.assertTrue(self.main_branch.is_active, "Main Branch must remain active.")

    def test_non_main_branch_can_be_deactivated(self):
        c = self._client()
        c.post(reverse("restaurant:branch_toggle", args=[self.city_branch.id]))
        self.city_branch.refresh_from_db()
        self.assertFalse(self.city_branch.is_active)

    def test_non_main_branch_can_be_reactivated(self):
        self.city_branch.is_active = False
        self.city_branch.save(update_fields=["is_active"])
        c = self._client()
        c.post(reverse("restaurant:branch_toggle", args=[self.city_branch.id]))
        self.city_branch.refresh_from_db()
        self.assertTrue(self.city_branch.is_active)


class BranchDeleteTests(TestCase):
    """Main Branch cannot be deleted. Branch with order history cannot be deleted."""

    @classmethod
    def setUpTestData(cls):
        cls.restaurant = Restaurant.objects.create(name="Delete Test Diner", address="Dhaka")
        cls.owner = _make_restaurant_owner("del_owner", cls.restaurant)
        cls.main_branch = Branch.objects.create(
            restaurant=cls.restaurant, name="Main", code="MAIN", is_main=True
        )
        cls.empty_branch = Branch.objects.create(
            restaurant=cls.restaurant, name="Empty", code="EMPT"
        )
        cls.order_branch = Branch.objects.create(
            restaurant=cls.restaurant, name="WithOrders", code="WO01"
        )
        # Create a historical order on order_branch
        Order.objects.create(
            restaurant=cls.restaurant,
            branch=cls.order_branch,
            order_type="TAKEAWAY",
            status="COMPLETED",
            total_amount=Decimal("100.00"),
        )

    def _client(self):
        c = Client()
        c.force_login(self.owner)
        return c

    def test_main_branch_cannot_be_deleted(self):
        c = self._client()
        c.post(reverse("restaurant:branch_delete", args=[self.main_branch.id]))
        self.assertTrue(Branch.objects.filter(pk=self.main_branch.pk).exists())

    def test_branch_with_orders_cannot_be_deleted(self):
        c = self._client()
        resp = c.post(reverse("restaurant:branch_delete", args=[self.order_branch.id]))
        self.assertRedirects(resp, reverse("restaurant:branch_list"))
        self.assertTrue(Branch.objects.filter(pk=self.order_branch.pk).exists())

    def test_empty_branch_can_be_deleted(self):
        c = self._client()
        c.post(reverse("restaurant:branch_delete", args=[self.empty_branch.id]))
        self.assertFalse(Branch.objects.filter(pk=self.empty_branch.pk).exists())


class SwitchBranchTests(TestCase):
    """Owner can switch active branch via POST. Staff cannot."""

    @classmethod
    def setUpTestData(cls):
        cls.restaurant = Restaurant.objects.create(name="Switch Test Diner", address="Dhaka")
        cls.main_branch = Branch.objects.create(
            restaurant=cls.restaurant, name="Main", code="MAIN", is_main=True
        )
        cls.city_branch = Branch.objects.create(
            restaurant=cls.restaurant, name="City", code="CTY"
        )
        cls.owner = _make_restaurant_owner("switch_owner", cls.restaurant)
        cls.waiter = _make_staff("switch_waiter", cls.restaurant, branch=cls.main_branch)

    def test_owner_can_switch_branch(self):
        c = Client()
        c.force_login(self.owner)
        resp = c.post(
            reverse("restaurant:switch_branch"),
            {"branch_id": self.city_branch.id},
        )
        # Should redirect (to branch_list)
        self.assertEqual(resp.status_code, 302)
        # Session must be updated
        session = c.session
        self.assertEqual(session.get("active_branch_id"), self.city_branch.id)

    def test_staff_cannot_switch_branch(self):
        c = Client()
        c.force_login(self.waiter)
        resp = c.post(
            reverse("restaurant:switch_branch"),
            {"branch_id": self.city_branch.id},
        )
        self.assertEqual(resp.status_code, 403)

    def test_switch_to_inactive_branch_rejected(self):
        inactive = Branch.objects.create(
            restaurant=self.restaurant, name="Inactive", code="INAC", is_active=False
        )
        c = Client()
        c.force_login(self.owner)
        resp = c.post(
            reverse("restaurant:switch_branch"),
            {"branch_id": inactive.id},
        )
        # Must redirect with error — active_branch_id in session should NOT be the inactive one
        self.assertEqual(resp.status_code, 302)
        session = c.session
        self.assertNotEqual(session.get("active_branch_id"), inactive.id)

    def test_switch_next_redirect(self):
        c = Client()
        c.force_login(self.owner)
        resp = c.post(
            reverse("restaurant:switch_branch"),
            {"branch_id": self.city_branch.id, "next": "/restaurant-management/branches/"},
        )
        self.assertRedirects(resp, "/restaurant-management/branches/", fetch_redirect_response=False)

    def test_open_redirect_blocked(self):
        c = Client()
        c.force_login(self.owner)
        resp = c.post(
            reverse("restaurant:switch_branch"),
            {"branch_id": self.city_branch.id, "next": "//evil.com/steal"},
        )
        # Must NOT redirect to the external URL
        self.assertNotIn("evil.com", resp.get("Location", ""))


class BranchFormUnitTests(TestCase):
    """Unit tests for BranchForm validation logic."""

    @classmethod
    def setUpTestData(cls):
        cls.restaurant = Restaurant.objects.create(name="Form Test Diner", address="Dhaka")
        Branch.objects.create(restaurant=cls.restaurant, name="Existing", code="EXS", is_main=True)

    def test_valid_form(self):
        form = BranchForm(
            {"name": "New Branch", "code": "NB1", "address": "", "phone": "", "email": ""},
            restaurant=self.restaurant,
        )
        self.assertTrue(form.is_valid())

    def test_code_uppercased_on_clean(self):
        form = BranchForm(
            {"name": "Lower Code", "code": "low", "address": "", "phone": "", "email": ""},
            restaurant=self.restaurant,
        )
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["code"], "LOW")

    def test_duplicate_name_invalid(self):
        form = BranchForm(
            {"name": "Existing", "code": "XX1", "address": "", "phone": "", "email": ""},
            restaurant=self.restaurant,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("name", form.errors)

    def test_duplicate_code_invalid(self):
        form = BranchForm(
            {"name": "Totally New", "code": "EXS", "address": "", "phone": "", "email": ""},
            restaurant=self.restaurant,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("code", form.errors)

    def test_duplicate_ignored_for_same_instance(self):
        branch = Branch.objects.get(restaurant=self.restaurant, code="EXS")
        form = BranchForm(
            {"name": "Existing", "code": "EXS", "address": "Updated address", "phone": "", "email": ""},
            instance=branch,
            restaurant=self.restaurant,
        )
        self.assertTrue(form.is_valid(), form.errors)


class ContextProcessorTests(TestCase):
    """active_branch_context exposes active_branch, user_branches, can_switch_branch."""

    @classmethod
    def setUpTestData(cls):
        cls.restaurant = Restaurant.objects.create(name="CTX Test Diner", address="Dhaka")
        cls.main_branch = Branch.objects.create(
            restaurant=cls.restaurant, name="Main", code="MAIN", is_main=True
        )
        cls.owner = _make_restaurant_owner("ctx_owner", cls.restaurant)
        cls.waiter = _make_staff("ctx_waiter", cls.restaurant, branch=cls.main_branch)

    def test_owner_can_switch_branch_flag(self):
        c = Client()
        c.force_login(self.owner)
        resp = c.get(reverse("restaurant:branch_list"))
        self.assertTrue(resp.context.get("can_switch_branch"))

    def test_staff_cannot_switch_branch_flag(self):
        # Staff can't reach branch_list (403), so test via a page they can access
        c = Client()
        c.force_login(self.waiter)
        resp = c.get(reverse("pos_dashboard"))
        self.assertFalse(resp.context.get("can_switch_branch"))

    def test_owner_gets_user_branches(self):
        c = Client()
        c.force_login(self.owner)
        resp = c.get(reverse("restaurant:branch_list"))
        branches = resp.context.get("user_branches", [])
        branch_ids = [b.id for b in branches]
        self.assertIn(self.main_branch.id, branch_ids)

    def test_active_branch_defaults_to_main(self):
        c = Client()
        c.force_login(self.owner)
        resp = c.get(reverse("restaurant:branch_list"))
        active = resp.context.get("active_branch")
        self.assertIsNotNone(active)
        self.assertEqual(active.id, self.main_branch.id)
