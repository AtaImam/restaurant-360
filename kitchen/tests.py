from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from inventory.models import (
    BranchIngredientStock,
    Ingredient,
    IngredientCategory,
    IngredientRequest,
    PurchaseOrder,
    PurchaseOrderItem,
    Recipe,
    RecipeIngredient,
    Supplier,
)
from menu.models import BranchMenuItemOverride, Category, MenuItem
from orders.models import Order, OrderItem
from orders.services import transition_order_status
from restaurant.models import Branch, Restaurant, Table
from staff.models import (
    Attendance,
    DailyTableAssignment,
    EmployeeProfile,
    LeaveRequest,
    OrderStaffService,
    PayrollRecord,
    SalaryAdvance,
    Shift,
    StaffNotification,
)
from staff.operations import assign_table_to_waiter, register_new_order

User = get_user_model()


class KitchenPortalCompleteTests(TestCase):
    def setUp(self):
        self.client = Client()

        self.restaurant = Restaurant.objects.create(
            name="Grand Kitchen",
            address="100 Chef Boulevard",
            phone="01711111111",
        )

        self.branch = Branch.objects.create(
            restaurant=self.restaurant,
            name="Downtown Kitchen",
            is_main=True,
        )

        self.shift = Shift.objects.create(
            restaurant=self.restaurant,
            branch=self.branch,
            name="Kitchen Shift",
            start_time=time(0, 0),
            end_time=time(23, 59),
        )

        # 1. Owner User
        self.owner = User.objects.create_user(
            username="owner_chef",
            password="password123",
            role="owner",
            is_active=True,
            is_active_staff=True,
            restaurant=self.restaurant,
            branch=self.branch,
        )

        self.manager = User.objects.create_user(
            username="manager_mike",
            password="password123",
            role="manager",
            is_active=True,
            is_active_staff=True,
            restaurant=self.restaurant,
            branch=self.branch,
        )

        # 2. Chef User (role: chief)
        self.chef = User.objects.create_user(
            username="head_chef_tony",
            password="password123",
            role="chief",
            is_active=True,
            is_active_staff=True,
            restaurant=self.restaurant,
            branch=self.branch,
            first_name="Tony",
            last_name="Bourdain",
        )
        self.chef_profile = EmployeeProfile.objects.create(
            user=self.chef,
            employee_id="CH-01",
            joining_date=date(2026, 1, 1),
            basic_salary=Decimal("50000.00"),
            shift=self.shift,
        )

        # 3. Kitchen Manager User (role: kitchen_manager)
        self.kitchen_mgr = User.objects.create_user(
            username="km_gordon",
            password="password123",
            role="kitchen_manager",
            is_active=True,
            is_active_staff=True,
            restaurant=self.restaurant,
            branch=self.branch,
            first_name="Gordon",
            last_name="Ramsay",
        )
        self.km_profile = EmployeeProfile.objects.create(
            user=self.kitchen_mgr,
            employee_id="KM-01",
            joining_date=date(2026, 1, 1),
            basic_salary=Decimal("55000.00"),
            shift=self.shift,
        )

        # 4. Waiter User (role: waiter)
        self.waiter = User.objects.create_user(
            username="waiter_alex",
            password="password123",
            role="waiter",
            is_active=True,
            is_active_staff=True,
            restaurant=self.restaurant,
            branch=self.branch,
            first_name="Alex",
            last_name="Smith",
        )
        self.waiter_profile = EmployeeProfile.objects.create(
            user=self.waiter,
            employee_id="W-01",
            joining_date=date(2026, 1, 1),
            basic_salary=Decimal("30000.00"),
            shift=self.shift,
        )

        # Table
        self.table = Table.objects.create(
            restaurant=self.restaurant,
            branch=self.branch,
            table_number=5,
            capacity=4,
        )

        # Waiter attendance and assigned table today
        now = timezone.now()
        self.waiter_attendance = Attendance.objects.create(
            employee=self.waiter_profile,
            restaurant=self.restaurant,
            branch=self.branch,
            shift=self.shift,
            work_date=timezone.localdate(),
            scheduled_start=now - timedelta(hours=1),
            scheduled_end=now + timedelta(hours=7),
            check_in=now - timedelta(hours=1),
        )
        self.assignment = DailyTableAssignment.objects.create(
            work_date=timezone.localdate(),
            restaurant=self.restaurant,
            table=self.table,
            waiter=self.waiter_profile,
            attendance=self.waiter_attendance,
            assigned_by=self.owner,
            is_active=True,
        )

        # Menu & Category
        self.category = Category.objects.create(
            restaurant=self.restaurant,
            name="Main Courses",
        )
        self.burger = MenuItem.objects.create(
            category=self.category,
            name="Artisan Burger",
            price=Decimal("450.00"),
            is_available=True,
        )

        # Ingredient & Recipe
        self.ing_cat = IngredientCategory.objects.create(
            restaurant=self.restaurant,
            name="Meat & Poultry",
        )
        self.beef_patty = Ingredient.objects.create(
            restaurant=self.restaurant,
            category=self.ing_cat,
            name="Beef Patty",
            sku="BEEF-001",
            base_unit="PCS",
            pack_size=Decimal("1.000"),
            current_pack_price=Decimal("120.00"),
            current_stock=Decimal("20.000"),
        )
        self.stock, _ = BranchIngredientStock.objects.get_or_create(
            branch=self.branch,
            ingredient=self.beef_patty,
        )
        self.stock.current_stock = Decimal("20.000")
        self.stock.reserved_stock = Decimal("0.000")
        self.stock.min_stock_alert = Decimal("5.000")
        self.stock.save()

        self.recipe = Recipe.objects.create(
            menu_item=self.burger,
            yield_quantity=Decimal("1.00"),
            instructions="Grill patty 4 minutes per side.",
        )
        self.recipe_ing = RecipeIngredient.objects.create(
            recipe=self.recipe,
            ingredient=self.beef_patty,
            quantity=Decimal("1.000"),
        )

        # Supplier
        self.supplier = Supplier.objects.create(
            restaurant=self.restaurant,
            name="Prime Meats Ltd",
            contact_name="John Doe",
            phone="01800000000",
        )

    # -----------------------------------------------------------------------
    # 1. KDS & Order Status Progression
    # -----------------------------------------------------------------------
    def test_kds_access_control(self):
        # Customer is denied from kitchen
        customer = User.objects.create_user(username="cust_1", role="customer", password="password123")
        self.client.force_login(customer)
        res_cust = self.client.get(reverse("kitchen_dashboard"))
        self.assertEqual(res_cust.status_code, 403)

        # Chef can access and is operator
        self.client.force_login(self.chef)
        res_chef = self.client.get(reverse("kitchen_dashboard"))
        self.assertEqual(res_chef.status_code, 200)
        self.assertTrue(res_chef.context["is_kitchen_operator"])
        self.assertFalse(res_chef.context["is_read_only"])

        # Kitchen Manager can access and is operator
        self.client.force_login(self.kitchen_mgr)
        res_km = self.client.get(reverse("kitchen_dashboard"))
        self.assertEqual(res_km.status_code, 200)
        self.assertTrue(res_km.context["is_kitchen_operator"])
        self.assertFalse(res_km.context["is_read_only"])

        # Owner can access but is READ-ONLY monitor
        self.client.force_login(self.owner)
        res_owner = self.client.get(reverse("kitchen_dashboard"))
        self.assertEqual(res_owner.status_code, 200)
        self.assertFalse(res_owner.context["is_kitchen_operator"])
        self.assertTrue(res_owner.context["is_read_only"])

        # Manager can access but is READ-ONLY monitor
        self.client.force_login(self.manager)
        res_mgr = self.client.get(reverse("kitchen_dashboard"))
        self.assertEqual(res_mgr.status_code, 200)
        self.assertFalse(res_mgr.context["is_kitchen_operator"])
        self.assertTrue(res_mgr.context["is_read_only"])

    def test_owner_and_manager_see_readonly_kitchen_monitor(self):
        # Create a new order with items and waiter assignment
        order1 = Order.objects.create(
            restaurant=self.restaurant,
            branch=self.branch,
            table=self.table,
            order_type="DINE_IN",
            status="NEW",
            total_amount=Decimal("450.00"),
        )
        OrderItem.objects.create(
            order=order1,
            menu_item=self.burger,
            quantity=2,
            price=Decimal("450.00"),
        )
        register_new_order(order1.id)

        # Create a second takeaway order in PREPARING
        order2 = Order.objects.create(
            restaurant=self.restaurant,
            branch=self.branch,
            order_type="TAKEAWAY",
            status="PREPARING",
            total_amount=Decimal("450.00"),
        )
        OrderItem.objects.create(
            order=order2,
            menu_item=self.burger,
            quantity=1,
            price=Decimal("450.00"),
        )
        register_new_order(order2.id)

        for user in [self.owner, self.manager]:
            self.client.force_login(user)

            # 1. Single Simple Read-Only Monitor Page
            res_dash = self.client.get(reverse("kitchen_dashboard"))
            self.assertEqual(res_dash.status_code, 200)
            self.assertContains(res_dash, "Kitchen Monitor")
            self.assertContains(res_dash, "Read-Only Monitor")
            # Orders shown
            self.assertContains(res_dash, f"#{order1.id}")
            self.assertContains(res_dash, f"#{order2.id}")
            # Current status shown
            self.assertContains(res_dash, "NEW")
            self.assertContains(res_dash, "PREPARING")
            # Table & Takeaway badges shown
            self.assertContains(res_dash, "Table 5")
            self.assertContains(res_dash, "Takeaway")
            # Waiter shown
            self.assertContains(res_dash, "Alex Smith")
            # Items shown
            self.assertContains(res_dash, "Artisan Burger")

            # Must NOT contain status change buttons or form actions
            self.assertNotContains(res_dash, "Accept Order")
            self.assertNotContains(res_dash, "Start Preparing")
            self.assertNotContains(res_dash, "Mark Ready")
            self.assertNotContains(res_dash, 'action="/kitchen/order/')

            # Must NOT contain sidebar or extra dashboard widgets (KPI grid, trend chart, stock panel)
            self.assertNotContains(res_dash, 'class="kitchen-sidebar"')
            self.assertNotContains(res_dash, 'id="sidebarToggle"')
            self.assertNotContains(res_dash, 'class="kpi-grid"')
            self.assertNotContains(res_dash, "7-Day Kitchen Order & Preparation Trend")
            self.assertNotContains(res_dash, "Low-Stock Summary")
            self.assertNotContains(res_dash, "Switch to Table View")

            # 2. All other kitchen pages are removed/redirected to single dashboard
            res_queue = self.client.get(reverse("kitchen_order_queue"))
            self.assertEqual(res_queue.status_code, 302)
            self.assertRedirects(res_queue, reverse("kitchen_dashboard"))

            res_history = self.client.get(reverse("kitchen_order_history"))
            self.assertEqual(res_history.status_code, 302)
            self.assertRedirects(res_history, reverse("kitchen_dashboard"))

            res_recipes = self.client.get(reverse("kitchen_recipes"))
            self.assertEqual(res_recipes.status_code, 302)
            self.assertRedirects(res_recipes, reverse("kitchen_dashboard"))

    def test_chief_and_km_see_interactive_kds_actions(self):
        order = Order.objects.create(
            restaurant=self.restaurant,
            branch=self.branch,
            table=self.table,
            order_type="DINE_IN",
            status="NEW",
            total_amount=Decimal("450.00"),
        )
        OrderItem.objects.create(
            order=order,
            menu_item=self.burger,
            quantity=1,
            price=Decimal("450.00"),
        )
        register_new_order(order.id)

        for user in [self.chef, self.kitchen_mgr]:
            self.client.force_login(user)

            # In dashboard, sees "Accept Order" action, sidebar, and KPI cards
            res_dash = self.client.get(reverse("kitchen_dashboard"))
            self.assertEqual(res_dash.status_code, 200)
            self.assertContains(res_dash, "Accept Order")
            self.assertContains(res_dash, f'action="{reverse("update_order_status", args=[order.id])}"')
            self.assertContains(res_dash, 'class="kitchen-sidebar"')
            self.assertContains(res_dash, 'id="sidebarToggle"')
            self.assertContains(res_dash, 'class="kpi-grid"')
            self.assertContains(res_dash, "7-Day Kitchen Order & Preparation Trend")

            # In queue, sees "Accept" action button and full portal page (200 OK)
            res_queue = self.client.get(reverse("kitchen_order_queue"))
            self.assertEqual(res_queue.status_code, 200)
            self.assertContains(res_queue, "Accept")
            self.assertContains(res_queue, f'action="{reverse("update_order_status", args=[order.id])}"')

            # Can access other kitchen pages without redirect
            res_recipes = self.client.get(reverse("kitchen_recipes"))
            self.assertEqual(res_recipes.status_code, 200)

    def test_owner_and_manager_cannot_post_status_updates_backend_enforced(self):
        order = Order.objects.create(
            restaurant=self.restaurant,
            branch=self.branch,
            table=self.table,
            order_type="DINE_IN",
            status="NEW",
            total_amount=Decimal("450.00"),
        )
        OrderItem.objects.create(
            order=order,
            menu_item=self.burger,
            quantity=1,
            price=Decimal("450.00"),
        )
        register_new_order(order.id)

        # Owner attempting to change status is rejected with 403
        self.client.force_login(self.owner)
        res_owner = self.client.post(reverse("update_order_status", args=[order.id]), {"status": "ACCEPTED"})
        self.assertEqual(res_owner.status_code, 403)
        order.refresh_from_db()
        self.assertEqual(order.status, "NEW")

        # Manager attempting to change status is rejected with 403
        self.client.force_login(self.manager)
        res_mgr = self.client.post(reverse("update_order_status", args=[order.id]), {"status": "ACCEPTED"})
        self.assertEqual(res_mgr.status_code, 403)
        order.refresh_from_db()
        self.assertEqual(order.status, "NEW")

        # Waiter attempting to change status is rejected with 403
        self.client.force_login(self.waiter)
        res_w = self.client.post(reverse("update_order_status", args=[order.id]), {"status": "ACCEPTED"})
        self.assertEqual(res_w.status_code, 403)
        order.refresh_from_db()
        self.assertEqual(order.status, "NEW")

        # Chef can accept order
        self.client.force_login(self.chef)
        res_chef = self.client.post(reverse("update_order_status", args=[order.id]), {"status": "ACCEPTED"})
        self.assertEqual(res_chef.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, "ACCEPTED")

        # Kitchen Manager can start prep
        self.client.force_login(self.kitchen_mgr)
        res_km = self.client.post(reverse("update_order_status", args=[order.id]), {"status": "PREPARING"})
        self.assertEqual(res_km.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, "PREPARING")

        # Chef can mark ready
        res_ready = self.client.post(reverse("update_order_status", args=[order.id]), {"status": "READY"})
        self.assertEqual(res_ready.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, "READY")

    def test_branch_scoping_preserved_in_kitchen_monitor(self):
        from restaurant.models import Branch
        branch2 = Branch.objects.create(restaurant=self.restaurant, name="Uptown Branch", code="UPTOWN")

        order_b1 = Order.objects.create(
            restaurant=self.restaurant,
            branch=self.branch,
            order_type="TAKEAWAY",
            status="NEW",
            total_amount=Decimal("100.00"),
        )
        order_b2 = Order.objects.create(
            restaurant=self.restaurant,
            branch=branch2,
            order_type="TAKEAWAY",
            status="NEW",
            total_amount=Decimal("200.00"),
        )

        # Chef is assigned to self.branch
        self.client.force_login(self.chef)
        resp_chef = self.client.get(reverse("kitchen_dashboard"))
        orders_chef = resp_chef.context["orders"]
        self.assertIn(order_b1, orders_chef)
        self.assertNotIn(order_b2, orders_chef)

        # Owner switches active branch in session
        self.client.force_login(self.owner)
        session = self.client.session
        session["active_branch_id"] = branch2.id
        session.save()

        resp_owner = self.client.get(reverse("kitchen_dashboard"))
        orders_owner = resp_owner.context["orders"]
        self.assertIn(order_b2, orders_owner)
        self.assertNotIn(order_b1, orders_owner)

    def test_kds_order_lifecycle_and_waiter_notification(self):
        # Create a new order on Table 5
        order = Order.objects.create(
            restaurant=self.restaurant,
            branch=self.branch,
            table=self.table,
            order_type="DINE_IN",
            status="NEW",
            total_amount=Decimal("450.00"),
        )
        OrderItem.objects.create(
            order=order,
            menu_item=self.burger,
            quantity=1,
            price=Decimal("450.00"),
        )
        register_new_order(order.id)

        self.client.force_login(self.chef)

        # Step 1: Advance NEW -> ACCEPTED
        res1 = self.client.post(reverse("update_order_status", args=[order.id]), {"status": "ACCEPTED"})
        self.assertEqual(res1.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, "ACCEPTED")

        # Step 2: Advance ACCEPTED -> PREPARING
        res2 = self.client.post(reverse("update_order_status", args=[order.id]), {"status": "PREPARING"})
        self.assertEqual(res2.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, "PREPARING")

        # Step 3: Advance PREPARING -> READY
        res3 = self.client.post(reverse("update_order_status", args=[order.id]), {"status": "READY"})
        self.assertEqual(res3.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, "READY")

        # Verify Waiter received READY notification
        notifs = StaffNotification.objects.filter(recipient=self.waiter, notification_type="food_ready")
        self.assertTrue(notifs.exists())
        self.assertIn("ready", notifs.first().title.lower())

    def test_invalid_status_transitions_rejected(self):
        order = Order.objects.create(
            restaurant=self.restaurant,
            branch=self.branch,
            table=self.table,
            order_type="DINE_IN",
            status="NEW",
            total_amount=Decimal("450.00"),
        )
        self.client.force_login(self.chef)

        # Cannot jump straight from NEW to READY
        self.client.post(reverse("update_order_status", args=[order.id]), {"status": "READY"})
        order.refresh_from_db()
        self.assertEqual(order.status, "NEW")

        # Cannot mark as COMPLETED from kitchen endpoint
        self.client.post(reverse("update_order_status", args=[order.id]), {"status": "COMPLETED"})
        order.refresh_from_db()
        self.assertEqual(order.status, "NEW")

    # -----------------------------------------------------------------------
    # 2. Menu Items & Availability / Sold Out Toggle
    # -----------------------------------------------------------------------
    def test_menu_items_and_toggle_sold_out(self):
        self.client.force_login(self.chef)

        # View menu items page
        res = self.client.get(reverse("kitchen_menu_items"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Artisan Burger")

        # Toggle to Sold Out in branch
        toggle_res = self.client.post(reverse("toggle_item_availability", args=[self.burger.id]))
        self.assertEqual(toggle_res.status_code, 302)

        override = BranchMenuItemOverride.objects.get(branch=self.branch, menu_item=self.burger)
        self.assertFalse(override.is_available)

        # Toggle back to Available
        self.client.post(reverse("toggle_item_availability", args=[self.burger.id]))
        override.refresh_from_db()
        self.assertTrue(override.is_available)

    # -----------------------------------------------------------------------
    # 3. Recipes, Ingredients, and Portions Calculation
    # -----------------------------------------------------------------------
    def test_recipes_and_portions_calculation(self):
        self.client.force_login(self.chef)

        res_recipes = self.client.get(reverse("kitchen_recipes"))
        self.assertEqual(res_recipes.status_code, 200)
        self.assertContains(res_recipes, "Artisan Burger")
        self.assertContains(res_recipes, "20 Portions Possible")

        res_ing = self.client.get(reverse("kitchen_ingredients"))
        self.assertEqual(res_ing.status_code, 200)
        self.assertContains(res_ing, "Beef Patty")

    def test_low_stock_alert_view(self):
        self.client.force_login(self.chef)

        # With 20 in stock and 5 min alert, it is NOT low
        res1 = self.client.get(reverse("kitchen_low_stock"))
        self.assertEqual(res1.status_code, 200)
        self.assertNotContains(res1, "Beef Patty")

        # Reduce stock to 3 (below min alert 5)
        self.stock.current_stock = Decimal("3.000")
        self.stock.save()

        res2 = self.client.get(reverse("kitchen_low_stock"))
        self.assertEqual(res2.status_code, 200)
        self.assertContains(res2, "Beef Patty")
        self.assertContains(res2, "Low Stock")

    # -----------------------------------------------------------------------
    # 4. Ingredient Request Full Lifecycle
    # -----------------------------------------------------------------------
    def test_ingredient_request_lifecycle(self):
        # 1. Chef submits an Ingredient Request
        self.client.force_login(self.chef)
        create_res = self.client.post(reverse("kitchen_ingredient_requests"), {
            "ingredient_id": self.beef_patty.id,
            "quantity": "25.000",
            "priority": "HIGH",
            "needed_date": str(date.today() + timedelta(days=2)),
            "reason": "Preparing for weekend rush",
        })
        self.assertEqual(create_res.status_code, 302)

        req = IngredientRequest.objects.get(ingredient=self.beef_patty)
        self.assertEqual(req.status, IngredientRequest.Status.PENDING)
        self.assertEqual(req.quantity, Decimal("25.000"))
        self.assertEqual(req.requested_by, self.chef)

        # 2. Owner approves the request
        self.client.force_login(self.owner)
        appr_res = self.client.post(reverse("approve_ingredient_request", args=[req.id]))
        self.assertEqual(appr_res.status_code, 302)
        req.refresh_from_db()
        self.assertEqual(req.status, IngredientRequest.Status.APPROVED)
        self.assertEqual(req.reviewed_by, self.owner)

        # 3. Owner converts the request to a Purchase Order
        po_res = self.client.post(reverse("convert_request_to_po", args=[req.id]), {
            "supplier_id": self.supplier.id,
            "quantity": "25.000",
            "unit_price": "120.00",
            "notes": "Emergency restock",
        })
        self.assertEqual(po_res.status_code, 302)
        req.refresh_from_db()
        self.assertEqual(req.status, IngredientRequest.Status.ORDERED)
        self.assertIsNotNone(req.purchase_order)

        # 4. Receiving the Purchase Order marks request as RECEIVED
        po = req.purchase_order
        receive_res = self.client.post(reverse("purchase_receive", args=[po.id]))
        self.assertEqual(receive_res.status_code, 302)
        req.refresh_from_db()
        self.assertEqual(req.status, IngredientRequest.Status.RECEIVED)

    def test_ingredient_request_rejection(self):
        req = IngredientRequest.objects.create(
            restaurant=self.restaurant,
            branch=self.branch,
            ingredient=self.beef_patty,
            quantity=Decimal("10.000"),
            priority=IngredientRequest.Priority.LOW,
            requested_by=self.chef,
            status=IngredientRequest.Status.PENDING,
        )

        self.client.force_login(self.owner)
        rej_res = self.client.post(reverse("reject_ingredient_request", args=[req.id]), {
            "rejection_reason": "Stock already sufficient",
        })
        self.assertEqual(rej_res.status_code, 302)
        req.refresh_from_db()
        self.assertEqual(req.status, IngredientRequest.Status.REJECTED)
        self.assertEqual(req.rejection_reason, "Stock already sufficient")

    def test_kitchen_staff_can_cancel_pending_request(self):
        req = IngredientRequest.objects.create(
            restaurant=self.restaurant,
            branch=self.branch,
            ingredient=self.beef_patty,
            quantity=Decimal("5.000"),
            priority=IngredientRequest.Priority.LOW,
            requested_by=self.chef,
            status=IngredientRequest.Status.PENDING,
        )

        self.client.force_login(self.chef)
        cancel_res = self.client.post(reverse("kitchen_cancel_ingredient_request", args=[req.id]))
        self.assertEqual(cancel_res.status_code, 302)
        self.assertFalse(IngredientRequest.objects.filter(id=req.id).exists())

    # -----------------------------------------------------------------------
    # 5. Employee Self-Service for Kitchen Staff
    # -----------------------------------------------------------------------
    def test_kitchen_attendance_check_in_and_out(self):
        self.client.force_login(self.chef)

        # Set dynamic shift window relative to current time to avoid midnight boundary issues
        now_local = timezone.localtime()
        self.shift.start_time = (now_local - timedelta(hours=1)).time()
        self.shift.end_time = (now_local + timedelta(hours=7)).time()
        self.shift.save()

        # Check in
        in_res = self.client.post(reverse("kitchen_check_in"))
        self.assertEqual(in_res.status_code, 302)

        att = Attendance.objects.filter(employee=self.chef_profile).order_by("-id").first()
        self.assertIsNotNone(att)
        self.assertIsNotNone(att.check_in)
        self.assertIsNone(att.check_out)

        # Check out
        out_res = self.client.post(reverse("kitchen_check_out"))
        self.assertEqual(out_res.status_code, 302)

        att.refresh_from_db()
        self.assertIsNotNone(att.check_out)

    def test_kitchen_leave_and_salary_advance_requests(self):
        self.client.force_login(self.chef)

        # Submit leave
        leave_res = self.client.post(reverse("kitchen_leave"), {
            "leave_type": "sick",
            "start_date": str(date.today()),
            "end_date": str(date.today() + timedelta(days=1)),
            "reason": "Flu symptoms",
        })
        self.assertEqual(leave_res.status_code, 302)
        self.assertTrue(LeaveRequest.objects.filter(employee=self.chef_profile).exists())

        # Submit salary advance
        adv_res = self.client.post(reverse("kitchen_salary_advance"), {
            "amount": "5000.00",
            "reason": "Medical expenses",
        })
        self.assertEqual(adv_res.status_code, 302)
        self.assertTrue(SalaryAdvance.objects.filter(employee=self.chef_profile).exists())

    def test_kitchen_shift_and_salary_pages(self):
        self.client.force_login(self.chef)

        res_shift = self.client.get(reverse("kitchen_shift"))
        self.assertEqual(res_shift.status_code, 200)

        res_salary = self.client.get(reverse("kitchen_salary"))
        self.assertEqual(res_salary.status_code, 200)
        self.assertContains(res_salary, "50000.00")

        res_hist = self.client.get(reverse("kitchen_payroll_history"))
        self.assertEqual(res_hist.status_code, 200)

    # -----------------------------------------------------------------------
    # 6. Strict Permissions Enforcements
    # -----------------------------------------------------------------------
    def test_kitchen_role_forbidden_from_admin_and_finance(self):
        self.client.force_login(self.chef)

        # Forbidden from Finance
        res_fin = self.client.get(reverse("finance:expense_list"))
        self.assertIn(res_fin.status_code, [403, 302])

        # Forbidden from Purchase Orders list/create
        res_po_list = self.client.get(reverse("purchase_list"))
        self.assertEqual(res_po_list.status_code, 403)

        res_po_add = self.client.get(reverse("purchase_create"))
        self.assertEqual(res_po_add.status_code, 403)

        # Forbidden from Business Settings
        res_settings = self.client.get(reverse("business_settings:settings"))
        self.assertIn(res_settings.status_code, [403, 302])

        # Forbidden from Restaurant / Table Management
        res_mgmt = self.client.get(reverse("restaurant:table_management"))
        self.assertIn(res_mgmt.status_code, [403, 302])

        # Forbidden from Coupons
        res_coupon = self.client.get(reverse("coupon_list"))
        self.assertIn(res_coupon.status_code, [403, 302])

        # Forbidden from Staff Payroll Admin
        res_payroll_admin = self.client.get(reverse("staff:payroll_list"))
        self.assertIn(res_payroll_admin.status_code, [403, 302])

    # -----------------------------------------------------------------------
    # 7. Layout & Navigation Consistency & KPI Verification
    # -----------------------------------------------------------------------
    def test_kitchen_order_queue_and_owner_layout_redirection(self):
        # 1. Chief visiting owner orders list is redirected to kitchen queue
        self.client.force_login(self.chef)
        res_owner_orders = self.client.get(reverse("orders_list"))
        self.assertEqual(res_owner_orders.status_code, 302)
        self.assertEqual(res_owner_orders.url, reverse("kitchen_order_queue"))

        # 2. Kitchen Manager visiting owner orders list is also redirected
        self.client.force_login(self.kitchen_mgr)
        res_km_orders = self.client.get(reverse("orders_list"))
        self.assertEqual(res_km_orders.status_code, 302)
        self.assertEqual(res_km_orders.url, reverse("kitchen_order_queue"))

        # 3. Kitchen order queue renders cleanly with base_kitchen layout
        res_queue = self.client.get(reverse("kitchen_order_queue"))
        self.assertEqual(res_queue.status_code, 200)
        self.assertContains(res_queue, "Live Kitchen Order Queue")
        self.assertContains(res_queue, "brand-title")
        self.assertNotContains(res_queue, "Owner / Admin")

    def test_kitchen_dashboard_real_kpis_and_metrics(self):
        # Create an order
        order = Order.objects.create(
            restaurant=self.restaurant,
            branch=self.branch,
            table=self.table,
            order_type="DINE_IN",
            status="NEW",
            total_amount=Decimal("450.00"),
        )
        OrderItem.objects.create(
            order=order,
            menu_item=self.burger,
            quantity=1,
            price=Decimal("450.00"),
        )
        register_new_order(order.id)

        self.client.force_login(self.chef)
        res = self.client.get(reverse("kitchen_dashboard"))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.context["kpi_new"], 1)
        self.assertEqual(res.context["kpi_accepted"], 0)
        self.assertIn("trend_data", res.context)
        self.assertIn("low_stock_summary", res.context)
        self.assertIn("urgent_requests", res.context)
        self.assertContains(res, "Kitchen Operations Dashboard")
        self.assertContains(res, "Live Kitchen Display Queue")
        self.assertContains(res, "Auto-refresh")

    def test_all_kitchen_views_shared_by_chief_and_km(self):
        urls = [
            reverse("kitchen_dashboard"),
            reverse("kitchen_order_queue"),
            reverse("kitchen_order_history"),
            reverse("kitchen_menu_items"),
            reverse("kitchen_recipes"),
            reverse("kitchen_ingredients"),
            reverse("kitchen_low_stock"),
            reverse("kitchen_ingredient_requests"),
            reverse("kitchen_performance"),
            reverse("kitchen_shift"),
            reverse("kitchen_attendance"),
            reverse("kitchen_leave"),
            reverse("kitchen_salary"),
            reverse("kitchen_salary_advance"),
            reverse("kitchen_payroll_history"),
        ]

        for user in [self.chef, self.kitchen_mgr]:
            self.client.force_login(user)
            for u in urls:
                res = self.client.get(u)
                self.assertEqual(res.status_code, 200, f"Failed for {user.role} on {u}")
                self.assertContains(res, "kitchen-sidebar")

