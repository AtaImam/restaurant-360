from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from restaurant.models import Branch, Restaurant
from staff.forms import EmployeeAccountEditForm, EmployeeAccountForm
from staff.models import EmployeeProfile

User = get_user_model()


class StaffRoleAssignmentAndKitchenRoutingTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.restaurant = Restaurant.objects.create(
            name="Spice Palace",
            address="789 Gulshan Ave",
            phone="01711223344",
        )

        self.branch_a = Branch.objects.create(
            restaurant=self.restaurant,
            name="Gulshan Branch",
            code="GUL-01",
            is_main=True,
        )

        self.branch_b = Branch.objects.create(
            restaurant=self.restaurant,
            name="Dhanmondi Branch",
            code="DHN-01",
            is_main=False,
        )

        self.owner = User.objects.create_user(
            username="owner_user",
            password="Password123!",
            role="owner",
            is_active=True,
            is_active_staff=True,
            restaurant=self.restaurant,
            branch=self.branch_a,
        )

        self.manager = User.objects.create_user(
            username="manager_user",
            password="Password123!",
            role="manager",
            is_active=True,
            is_active_staff=True,
            restaurant=self.restaurant,
            branch=self.branch_a,
        )

        self.waiter = User.objects.create_user(
            username="waiter_user",
            password="Password123!",
            role="waiter",
            is_active=True,
            is_active_staff=True,
            restaurant=self.restaurant,
            branch=self.branch_a,
        )
        self.waiter_profile = EmployeeProfile.objects.create(
            user=self.waiter,
            employee_id="W-900",
            joining_date=date(2026, 1, 1),
            basic_salary=Decimal("20000.00"),
        )

        self.chef = User.objects.create_user(
            username="chef_user",
            password="Password123!",
            role="chief",
            is_active=True,
            is_active_staff=True,
            restaurant=self.restaurant,
            branch=self.branch_a,
        )
        self.chef_profile = EmployeeProfile.objects.create(
            user=self.chef,
            employee_id="C-900",
            joining_date=date(2026, 1, 1),
            basic_salary=Decimal("45000.00"),
        )

        self.kitchen_manager = User.objects.create_user(
            username="km_user",
            password="Password123!",
            role="kitchen_manager",
            is_active=True,
            is_active_staff=True,
            restaurant=self.restaurant,
            branch=self.branch_a,
        )
        self.km_profile = EmployeeProfile.objects.create(
            user=self.kitchen_manager,
            employee_id="KM-900",
            joining_date=date(2026, 1, 1),
            basic_salary=Decimal("48000.00"),
        )

    # -------------------------------------------------------------------------
    # Staff Role Assignment Form Tests
    # -------------------------------------------------------------------------

    def test_create_form_role_does_not_default_to_waiter(self):
        """EmployeeAccountForm should not default to waiter and require role selection."""
        form = EmployeeAccountForm(actor=self.owner, branch=self.branch_a)
        self.assertEqual(form.fields["role"].initial, "")
        self.assertEqual(form.initial.get("role"), "")
        self.assertTrue(form.fields["role"].required)

        # First choice is empty/placeholder
        choices = form.fields["role"].choices
        self.assertEqual(choices[0][0], "")

        # Unbound submission without role fails validation
        data = {
            "account-username": "norole_user",
            "account-first_name": "No",
            "account-last_name": "Role",
            "account-role": "",
            "account-password1": "StrongPass!123",
            "account-password2": "StrongPass!123",
            "profile-employee_id": "NR-01",
            "profile-joining_date": "2026-01-01",
            "profile-basic_salary": "15000",
        }
        bound_form = EmployeeAccountForm(data=data, actor=self.owner, branch=self.branch_a, prefix="account")
        self.assertFalse(bound_form.is_valid())
        self.assertIn("role", bound_form.errors)

    def test_owner_role_choices_in_create_form(self):
        """Owner can assign: manager, waiter, chief, kitchen_manager, bar_manager."""
        form = EmployeeAccountForm(actor=self.owner, branch=self.branch_a)
        role_values = [val for val, _ in form.fields["role"].choices if val]
        expected_roles = ["manager", "waiter", "chief", "kitchen_manager", "bar_manager"]
        self.assertEqual(role_values, expected_roles)
        self.assertNotIn("admin", role_values)
        self.assertNotIn("owner", role_values)

    def test_manager_cannot_assign_manager_or_higher_admin_owner_roles(self):
        """Manager can only assign waiter, chief, kitchen_manager, bar_manager."""
        form = EmployeeAccountForm(actor=self.manager, branch=self.branch_a)
        role_values = [val for val, _ in form.fields["role"].choices if val]
        expected_roles = ["waiter", "chief", "kitchen_manager", "bar_manager"]
        self.assertEqual(role_values, expected_roles)
        self.assertNotIn("manager", role_values)
        self.assertNotIn("admin", role_values)
        self.assertNotIn("owner", role_values)

    def test_owner_can_create_employee_with_any_allowed_role_and_branch(self):
        """Owner creates an employee assigned to branch_b and kitchen_manager role."""
        self.client.force_login(self.owner)
        response = self.client.post(reverse("staff:employee_create"), {
            "account-username": "chef_ramsay",
            "account-first_name": "Gordon",
            "account-last_name": "Ramsay",
            "account-role": "kitchen_manager",
            "account-branch": str(self.branch_b.pk),
            "account-password1": "StrongPass!123",
            "account-password2": "StrongPass!123",
            "profile-employee_id": "GR-01",
            "profile-joining_date": "2026-01-01",
            "profile-basic_salary": "60000",
        })
        self.assertEqual(response.status_code, 302)
        created_user = User.objects.get(username="chef_ramsay")
        self.assertEqual(created_user.role, "kitchen_manager")
        self.assertEqual(created_user.branch, self.branch_b)

    def test_edit_form_preserves_existing_role_and_branch(self):
        """Editing an employee preserves their existing role and branch."""
        edit_form = EmployeeAccountEditForm(
            instance=self.chef,
            actor=self.owner,
            branch=self.branch_a,
        )
        self.assertEqual(edit_form.initial.get("role"), "chief")
        self.assertEqual(edit_form.initial.get("branch"), self.branch_a)

        # Post edit without changing role or branch
        self.client.force_login(self.owner)
        response = self.client.post(reverse("staff:employee_edit", args=[self.chef.pk]), {
            "account-first_name": "Tony",
            "account-last_name": "Bourdain Updated",
            "account-email": "tony@example.com",
            "account-phone": "01700000000",
            "account-role": "chief",
            "account-branch": str(self.branch_a.pk),
            "profile-employee_id": "C-900",
            "profile-joining_date": "2026-01-01",
            "profile-basic_salary": "48000",
        })
        self.assertEqual(response.status_code, 302)
        self.chef.refresh_from_db()
        self.assertEqual(self.chef.role, "chief")
        self.assertEqual(self.chef.branch, self.branch_a)
        self.assertEqual(self.chef.last_name, "Bourdain Updated")

    def test_owner_can_change_role_and_branch_on_edit(self):
        """Owner can reassign role to bar_manager and move branch to branch_b."""
        self.client.force_login(self.owner)
        response = self.client.post(reverse("staff:employee_edit", args=[self.waiter.pk]), {
            "account-first_name": "Alex",
            "account-last_name": "Smith",
            "account-role": "bar_manager",
            "account-branch": str(self.branch_b.pk),
            "profile-employee_id": "W-900",
            "profile-joining_date": "2026-01-01",
            "profile-basic_salary": "25000",
        })
        self.assertEqual(response.status_code, 302)
        self.waiter.refresh_from_db()
        self.assertEqual(self.waiter.role, "bar_manager")
        self.assertEqual(self.waiter.branch, self.branch_b)

    def test_manager_cannot_promote_to_manager_on_edit(self):
        """Manager cannot edit a staff member to have the manager role."""
        self.client.force_login(self.manager)
        response = self.client.post(reverse("staff:employee_edit", args=[self.waiter.pk]), {
            "account-first_name": "Alex",
            "account-last_name": "Smith",
            "account-role": "manager",
            "account-branch": str(self.branch_a.pk),
            "profile-employee_id": "W-900",
            "profile-joining_date": "2026-01-01",
            "profile-basic_salary": "20000",
        })
        # Validation error: role cannot be manager
        self.waiter.refresh_from_db()
        self.assertEqual(self.waiter.role, "waiter")

    # -------------------------------------------------------------------------
    # Kitchen Dashboard Routing & Permissions Tests
    # -------------------------------------------------------------------------

    def test_get_dashboard_url_for_all_roles(self):
        """chief and kitchen_manager route to /kitchen/, owner and manager to /dashboard/."""
        self.assertEqual(self.chef.get_dashboard_url(), "/kitchen/")
        self.assertEqual(self.kitchen_manager.get_dashboard_url(), "/kitchen/")
        self.assertEqual(self.owner.get_dashboard_url(), "/dashboard/")
        self.assertEqual(self.manager.get_dashboard_url(), "/dashboard/")
        self.assertEqual(self.waiter.get_dashboard_url(), "/waiter/")

    def test_owner_can_view_kitchen_without_template_errors(self):
        """Owner viewing kitchen_dashboard succeeds with 200 and links back to owner_dashboard."""
        self.client.force_login(self.owner)
        response = self.client.get(reverse("kitchen_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("owner_dashboard"))
        self.assertContains(response, "Back to Admin")

    def test_manager_can_view_kitchen_without_template_errors(self):
        """Manager viewing kitchen_dashboard succeeds with 200 and links back to owner_dashboard."""
        self.client.force_login(self.manager)
        response = self.client.get(reverse("kitchen_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("owner_dashboard"))
        self.assertContains(response, "Back to Admin")

    def test_chef_and_kitchen_manager_view_kitchen_dashboard(self):
        """Chief and Kitchen Manager stay on /kitchen/ without admin back button."""
        for user in [self.chef, self.kitchen_manager]:
            self.client.force_login(user)
            response = self.client.get(reverse("kitchen_dashboard"))
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, "Back to Admin")
