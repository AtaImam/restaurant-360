import random
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from finance.models import Expense, ExpenseCategory
from finance.reports_services import get_profit_and_loss_data
from guests.models import OrderFeedback, Reservation
from inventory.models import (
    BranchIngredientStock,
    Ingredient,
    IngredientCategory,
    IngredientPriceHistory,
    IngredientRequest,
    PurchaseOrder,
    PurchaseOrderItem,
    Recipe,
    RecipeIngredient,
    StockCount,
    StockReservation,
    StockTransaction,
    StorageLocation,
    Supplier,
    WasteRecord,
)
from menu.models import Category, MenuItem
from orders.models import Order, OrderItem, PaymentTransaction, TableSession
from restaurant.models import Branch, Floor, Restaurant, Table
from staff.models import (
    Attendance,
    DailyTableAssignment,
    EmployeeProfile,
    LeaveRequest,
    OrderStaffService,
    PayrollRecord,
    SalaryAdvance,
    Shift,
    StaffTask,
)

User = get_user_model()
DEMO_PASSWORD = "Demo@12345"


class Command(BaseCommand):
    help = "Seed or reset a full realistic 90-day Restaurant 360 demo dataset for FoodHub"

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Safely remove demo-seeded data before re-seeding.",
        )
        parser.add_argument(
            "--restaurant-id",
            type=int,
            default=None,
            help="Target a specific restaurant ID instead of the default demo restaurant.",
        )

    def handle(self, *args, **options):
        reset = options.get("reset", False)
        restaurant_id = options.get("restaurant_id")

        self.stdout.write(
            self.style.WARNING(
                "=== Initializing Realistic 90-Day Restaurant 360 Demo Seed ==="
            )
        )

        # Fixed deterministic PRNG seed for reproducible and consistent demo numbers
        rng = random.Random(42)

        # Reference dates
        tz = timezone.get_current_timezone()
        now_local = timezone.localtime(timezone.now(), tz)
        today = now_local.date()
        start_date = today - timedelta(days=90)

        # -----------------------------------------------------
        # 1. RESOLVE DEMO RESTAURANT SAFELY
        # -----------------------------------------------------
        if restaurant_id:
            restaurant = Restaurant.objects.filter(pk=restaurant_id).first()
            if not restaurant:
                raise CommandError(f"Restaurant with ID {restaurant_id} not found.")
        else:
            restaurant = (
                Restaurant.objects.filter(
                    name="FoodHub", address__icontains="Dhanmondi"
                )
                .order_by("pk")
                .first()
                or Restaurant.objects.filter(pk=1, name="FoodHub").first()
                or Restaurant.objects.filter(name="FoodHub").order_by("pk").first()
            )
            if not restaurant:
                restaurant = Restaurant.objects.create(
                    name="FoodHub",
                    address="Dhanmondi, Dhaka",
                    phone="01700000000",
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"✓ Target Demo Restaurant: {restaurant.name} (ID: {restaurant.pk})"
            )
        )

        # Idempotency check: if not reset and demo data already seeded, report and return
        if not reset:
            order_count = Order.objects.filter(restaurant=restaurant).count()
            attendance_count = Attendance.objects.filter(restaurant=restaurant).count()
            if order_count >= 500 and attendance_count >= 100:
                self.stdout.write(
                    self.style.SUCCESS(
                        "✓ Realistic 90-day demo dataset is already seeded for this restaurant."
                    )
                )
                self.stdout.write(
                    self.style.NOTICE(
                        "  (Running idempotently without duplicates. Use --reset to reseed from scratch.)"
                    )
                )
                dhanmondi_branch = restaurant.branches.filter(is_main=True).first() or restaurant.branches.first()
                gulshan_branch = restaurant.branches.filter(code="GLS-01").first() or restaurant.branches.last()
                self._report_final_summary(restaurant, dhanmondi_branch, gulshan_branch, start_date, today, tz)
                return

        # -----------------------------------------------------
        # 2. SAFE RESET (REMOVES ONLY DEMO-SEEDED DATA FOR FOODHUB)
        # -----------------------------------------------------
        if reset:
            self.stdout.write(
                self.style.WARNING(
                    "Reset flag detected: removing demo-seeded operational data..."
                )
            )
            with transaction.atomic():
                # Guests & Orders
                OrderFeedback.objects.filter(order__restaurant=restaurant).delete()
                Reservation.objects.filter(restaurant=restaurant).delete()
                OrderStaffService.objects.filter(order__restaurant=restaurant).delete()
                DailyTableAssignment.objects.filter(restaurant=restaurant).delete()
                StaffTask.objects.filter(restaurant=restaurant).delete()
                PaymentTransaction.objects.filter(restaurant=restaurant).delete()
                OrderItem.objects.filter(order__restaurant=restaurant).delete()
                Order.objects.filter(restaurant=restaurant).delete()
                TableSession.objects.filter(restaurant=restaurant).delete()

                # Inventory movements & operations
                StockReservation.objects.filter(ingredient__restaurant=restaurant).delete()
                StockTransaction.objects.filter(ingredient__restaurant=restaurant).delete()
                WasteRecord.objects.filter(ingredient__restaurant=restaurant).delete()
                StockCount.objects.filter(ingredient__restaurant=restaurant).delete()
                IngredientRequest.objects.filter(ingredient__restaurant=restaurant).delete()
                PurchaseOrderItem.objects.filter(purchase_order__restaurant=restaurant).delete()
                PurchaseOrder.objects.filter(restaurant=restaurant).delete()
                Supplier.objects.filter(restaurant=restaurant).delete()

                # Finance
                Expense.objects.filter(restaurant=restaurant).delete()
                ExpenseCategory.objects.filter(restaurant=restaurant).delete()

                # Staff
                PayrollRecord.objects.filter(restaurant=restaurant).delete()
                SalaryAdvance.objects.filter(restaurant=restaurant).delete()
                Attendance.objects.filter(restaurant=restaurant).delete()
                LeaveRequest.objects.filter(restaurant=restaurant).delete()
                EmployeeProfile.objects.filter(
                    user__restaurant=restaurant, employee_id__startswith="DEMO-"
                ).delete()
                # Unlink shift on any remaining profiles before deleting shifts
                EmployeeProfile.objects.filter(user__restaurant=restaurant).update(shift=None)
                Shift.objects.filter(restaurant=restaurant).delete()

                # Demo Users (preserve superusers and non-demo existing users)
                User.objects.filter(
                    restaurant=restaurant, username__startswith="demo_"
                ).delete()

                # Reset table statuses to AVAILABLE
                Table.objects.filter(restaurant=restaurant).update(
                    status=Table.STATUS_AVAILABLE
                )

            self.stdout.write(self.style.SUCCESS("✓ Demo operational data reset complete."))

        # -----------------------------------------------------
        # 3. 2 BRANCHES & TABLES
        # -----------------------------------------------------
        with transaction.atomic():
            # Main Branch (Dhanmondi)
            dhanmondi_branch = restaurant.branches.filter(is_main=True).first() or restaurant.branches.first()
            if dhanmondi_branch:
                dhanmondi_branch.name = "Dhanmondi Branch"
                dhanmondi_branch.code = "DHN-01"
                dhanmondi_branch.address = "Road 27, Dhanmondi, Dhaka"
                dhanmondi_branch.phone = "01711000101"
                dhanmondi_branch.is_main = True
                dhanmondi_branch.save()
            else:
                dhanmondi_branch = Branch.objects.create(
                    restaurant=restaurant,
                    name="Dhanmondi Branch",
                    code="DHN-01",
                    address="Road 27, Dhanmondi, Dhaka",
                    phone="01711000101",
                    is_main=True,
                )

            # Secondary Branch (Gulshan)
            gulshan_branch = restaurant.branches.filter(code="GLS-01").first() or restaurant.branches.filter(
                name__icontains="Gulshan"
            ).first()
            if gulshan_branch:
                gulshan_branch.name = "Gulshan Branch"
                gulshan_branch.code = "GLS-01"
                gulshan_branch.address = "Gulshan 2, Dhaka"
                gulshan_branch.phone = "01711000102"
                gulshan_branch.is_main = False
                gulshan_branch.save()
            else:
                gulshan_branch = Branch.objects.create(
                    restaurant=restaurant,
                    name="Gulshan Branch",
                    code="GLS-01",
                    address="Gulshan 2, Dhaka",
                    phone="01711000102",
                    is_main=False,
                )

            # Floors
            floor_dhn, _ = Floor.objects.get_or_create(
                restaurant=restaurant,
                branch=dhanmondi_branch,
                name="Main Dining Floor",
                defaults={"floor_number": 1},
            )
            floor_gls, _ = Floor.objects.get_or_create(
                restaurant=restaurant,
                branch=gulshan_branch,
                name="Ground Floor",
                defaults={"floor_number": 1},
            )

            # Tables
            dhn_tables = []
            for num, cap in [(1, 2), (2, 4), (3, 4), (4, 6), (5, 4), (6, 4), (7, 2), (8, 8)]:
                t, _ = Table.objects.get_or_create(
                    restaurant=restaurant,
                    branch=dhanmondi_branch,
                    table_number=num,
                    defaults={"floor": floor_dhn, "capacity": cap, "status": Table.STATUS_AVAILABLE},
                )
                t.status = Table.STATUS_AVAILABLE
                t.save()
                dhn_tables.append(t)

            gls_tables = []
            for num, cap in [(1, 2), (2, 4), (3, 4), (4, 6), (5, 4), (6, 8)]:
                t, _ = Table.objects.get_or_create(
                    restaurant=restaurant,
                    branch=gulshan_branch,
                    table_number=num,
                    defaults={"floor": floor_gls, "capacity": cap, "status": Table.STATUS_AVAILABLE},
                )
                t.status = Table.STATUS_AVAILABLE
                t.save()
                gls_tables.append(t)

        self.stdout.write(
            self.style.SUCCESS(
                f"✓ 2 Branches verified: Dhanmondi ({len(dhn_tables)} tables), Gulshan ({len(gls_tables)} tables)"
            )
        )

        # -----------------------------------------------------
        # 4. STAFF, SHIFTS & ROLES
        # -----------------------------------------------------
        with transaction.atomic():
            # Shifts
            dhn_day_shift, _ = Shift.objects.get_or_create(
                restaurant=restaurant,
                branch=dhanmondi_branch,
                name="Day Shift",
                defaults={
                    "start_time": time(10, 0),
                    "end_time": time(18, 0),
                    "grace_minutes": 15,
                },
            )
            dhn_eve_shift, _ = Shift.objects.get_or_create(
                restaurant=restaurant,
                branch=dhanmondi_branch,
                name="Evening Shift",
                defaults={
                    "start_time": time(16, 0),
                    "end_time": time(23, 30),
                    "grace_minutes": 15,
                },
            )
            gls_day_shift, _ = Shift.objects.get_or_create(
                restaurant=restaurant,
                branch=gulshan_branch,
                name="Day Shift",
                defaults={
                    "start_time": time(10, 0),
                    "end_time": time(18, 0),
                    "grace_minutes": 15,
                },
            )
            gls_eve_shift, _ = Shift.objects.get_or_create(
                restaurant=restaurant,
                branch=gulshan_branch,
                name="Evening Shift",
                defaults={
                    "start_time": time(16, 0),
                    "end_time": time(23, 30),
                    "grace_minutes": 15,
                },
            )

            def get_or_create_user(username, first, last, role, branch, phone, salary, emp_id, shift):
                u = User.objects.filter(username=username).first()
                if not u:
                    u = User.objects.create_user(
                        username=username,
                        email=f"{username}@foodhub.demo",
                        password=DEMO_PASSWORD,
                        first_name=first,
                        last_name=last,
                        role=role,
                        phone=phone,
                        restaurant=restaurant,
                        branch=branch,
                        is_active_staff=True,
                    )
                else:
                    u.first_name = first
                    u.last_name = last
                    u.role = role
                    u.phone = phone
                    u.restaurant = restaurant
                    u.branch = branch
                    u.is_active_staff = True
                    u.set_password(DEMO_PASSWORD)
                    u.save()

                ep = getattr(u, "employee_profile", None)
                if not ep:
                    ep, _ = EmployeeProfile.objects.get_or_create(
                        user=u,
                        defaults={
                            "employee_id": emp_id,
                            "joining_date": today - timedelta(days=150),
                            "shift": shift,
                            "basic_salary": Decimal(str(salary)),
                        },
                    )
                else:
                    ep.employee_id = emp_id
                    ep.shift = shift
                    ep.basic_salary = Decimal(str(salary))
                    ep.save()
                return u, ep

            # Owner
            owner_user, _ = get_or_create_user(
                "demo_owner", "Arif", "Rahman", "owner", dhanmondi_branch,
                "01711000001", "50000.00", "DEMO-OWN-01", dhn_day_shift
            )

            # Dhanmondi staff (market-rate salaries)
            mgr_dhn, ep_mgr_dhn = get_or_create_user(
                "demo_mgr_dhn", "Tanvir", "Ahmed", "manager", dhanmondi_branch,
                "01711000002", "22000.00", "DEMO-DHN-01", dhn_day_shift
            )
            chef_dhn, ep_chef_dhn = get_or_create_user(
                "demo_chef_dhn", "Shakil", "Mia", "chief", dhanmondi_branch,
                "01711000003", "18000.00", "DEMO-DHN-02", dhn_day_shift
            )
            waiter_dhn1, ep_w_dhn1 = get_or_create_user(
                "demo_waiter_dhn1", "Nabil", "Hasan", "waiter", dhanmondi_branch,
                "01711000004", "11000.00", "DEMO-DHN-03", dhn_day_shift
            )
            waiter_dhn2, ep_w_dhn2 = get_or_create_user(
                "demo_waiter_dhn2", "Sadia", "Akter", "waiter", dhanmondi_branch,
                "01711000005", "11500.00", "DEMO-DHN-04", dhn_eve_shift
            )
            waiter_dhn3, ep_w_dhn3 = get_or_create_user(
                "demo_waiter_dhn3", "Rakib", "Hossain", "waiter", dhanmondi_branch,
                "01711000006", "10500.00", "DEMO-DHN-05", dhn_eve_shift
            )

            # Gulshan staff (market-rate salaries)
            mgr_gls, ep_mgr_gls = get_or_create_user(
                "demo_mgr_gls", "Farhan", "Kabir", "manager", gulshan_branch,
                "01711000007", "24000.00", "DEMO-GLS-01", gls_day_shift
            )
            chef_gls, ep_chef_gls = get_or_create_user(
                "demo_chef_gls", "Nusrat", "Jahan", "kitchen_manager", gulshan_branch,
                "01711000008", "19000.00", "DEMO-GLS-02", gls_day_shift
            )
            waiter_gls1, ep_w_gls1 = get_or_create_user(
                "demo_waiter_gls1", "Mim", "Islam", "waiter", gulshan_branch,
                "01711000009", "11500.00", "DEMO-GLS-03", gls_day_shift
            )
            waiter_gls2, ep_w_gls2 = get_or_create_user(
                "demo_waiter_gls2", "Kamal", "Uddin", "waiter", gulshan_branch,
                "01711000010", "11000.00", "DEMO-GLS-04", gls_eve_shift
            )

        all_employees = [
            ep_mgr_dhn, ep_chef_dhn, ep_w_dhn1, ep_w_dhn2, ep_w_dhn3,
            ep_mgr_gls, ep_chef_gls, ep_w_gls1, ep_w_gls2
        ]
        dhn_waiters = [ep_w_dhn1, ep_w_dhn2, ep_w_dhn3]
        gls_waiters = [ep_w_gls1, ep_w_gls2]

        self.stdout.write(
            self.style.SUCCESS(
                f"✓ Staff accounts created: 1 Owner, 2 Managers, 2 Chefs, 5 Waiters across 2 branches"
            )
        )

        # -----------------------------------------------------
        # 5. CORE MENU, CATEGORIES & INVENTORY FOUNDATION
        # -----------------------------------------------------
        with transaction.atomic():
            def get_or_create_cat(name):
                cat = Category.objects.filter(restaurant=restaurant, name=name).first()
                if not cat:
                    cat = Category.objects.create(restaurant=restaurant, name=name)
                return cat

            burger_cat = get_or_create_cat("Burger")
            pizza_cat = get_or_create_cat("Pizza")
            drinks_cat = get_or_create_cat("Drinks")
            biryani_cat = get_or_create_cat("Biryani & Rice")
            chinese_cat = get_or_create_cat("Chinese")

            def seed_menu_item(category, name, description, price):
                item = MenuItem.objects.filter(category=category, name=name).first()
                if not item:
                    item = MenuItem.objects.filter(
                        category__restaurant=restaurant, name=name
                    ).order_by("pk").first()

                if item:
                    item.category = category
                    item.description = description
                    item.price = Decimal(str(price))
                    item.is_available = True
                    item.save()
                else:
                    item = MenuItem.objects.create(
                        category=category,
                        name=name,
                        description=description,
                        price=Decimal(str(price)),
                        is_available=True,
                    )
                return item

            chicken_burger = seed_menu_item(
                burger_cat, "Chicken Burger", "Crispy chicken burger with cheese, lettuce and signature sauce.", "230.00"
            )
            beef_burger = seed_menu_item(
                burger_cat, "Beef Burger", "Juicy beef burger with cheese and fresh vegetables.", "250.00"
            )
            bbq_pizza = seed_menu_item(
                pizza_cat, "BBQ Pizza", "BBQ chicken pizza with mozzarella and smoky BBQ sauce.", "450.00"
            )
            mojo = seed_menu_item(
                drinks_cat, "Mojo", "Chilled soft drink.", "50.00"
            )
            chicken_biryani = seed_menu_item(
                biryani_cat, "Chicken Biryani", "Aromatic basmati rice cooked with chicken and spices.", "220.00"
            )
            beef_tehari = seed_menu_item(
                biryani_cat, "Beef Tehari", "Traditional mustard oil tehari with tender beef chunks.", "200.00"
            )
            mutton_biryani = seed_menu_item(
                biryani_cat, "Mutton Kacchi Biryani", "Special mutton kacchi biryani with aloo and egg.", "350.00"
            )
            chicken_fried_rice = seed_menu_item(
                chinese_cat, "Chicken Fried Rice", "Wok-tossed fried rice with diced chicken and egg.", "220.00"
            )
            masala_tea = seed_menu_item(
                drinks_cat, "Masala Tea", "Rich spiced milk tea.", "40.00"
            )
            mango_lassi = seed_menu_item(
                drinks_cat, "Mango Lassi", "Chilled sweet mango yogurt drink.", "90.00"
            )

            # Ingredient Categories & Storage
            def get_or_create_ing_cat(name):
                c = IngredientCategory.objects.filter(restaurant=restaurant, name=name).first()
                return c or IngredientCategory.objects.create(restaurant=restaurant, name=name)

            proteins = get_or_create_ing_cat("Proteins")
            dairy = get_or_create_ing_cat("Dairy")
            vegetables = get_or_create_ing_cat("Vegetables")
            bakery = get_or_create_ing_cat("Bakery")
            dry_goods = get_or_create_ing_cat("Dry Goods")
            sauces = get_or_create_ing_cat("Sauces")
            beverages = get_or_create_ing_cat("Beverages")

            def get_or_create_storage(name):
                s = StorageLocation.objects.filter(restaurant=restaurant, name=name).first()
                return s or StorageLocation.objects.create(restaurant=restaurant, name=name)

            freezer = get_or_create_storage("Freezer")
            refrigerator = get_or_create_storage("Refrigerator")
            dry_store = get_or_create_storage("Dry Store")
            beverage_store = get_or_create_storage("Beverage Store")

            def seed_ingredient(name, sku, category, location, unit, pack_size, pack_price, init_stock, min_lvl, tgt_lvl):
                ing = Ingredient.objects.filter(restaurant=restaurant, sku=sku).first()
                if not ing:
                    ing = Ingredient.objects.filter(restaurant=restaurant, name=name).first()

                pack_sz = Decimal(str(pack_size))
                pack_pr = Decimal(str(pack_price))
                stk = Decimal(str(init_stock))
                min_l = Decimal(str(min_lvl))
                tgt_l = Decimal(str(tgt_lvl))

                if ing:
                    ing.sku = sku
                    ing.name = name
                    ing.category = category
                    ing.storage_location = location
                    ing.base_unit = unit
                    ing.pack_size = pack_sz
                    ing.current_pack_price = pack_pr
                    ing.current_stock = stk
                    ing.minimum_level = min_l
                    ing.target_level = tgt_l
                    ing.is_active = True
                    ing.save()
                else:
                    ing = Ingredient.objects.create(
                        restaurant=restaurant,
                        sku=sku,
                        name=name,
                        category=category,
                        storage_location=location,
                        base_unit=unit,
                        pack_size=pack_sz,
                        current_pack_price=pack_pr,
                        current_stock=stk,
                        minimum_level=min_l,
                        target_level=tgt_l,
                        is_active=True,
                    )

                IngredientPriceHistory.objects.update_or_create(
                    ingredient=ing,
                    effective_date=today,
                    defaults={"pack_price": pack_pr},
                )

                for branch in [dhanmondi_branch, gulshan_branch]:
                    bis, _ = BranchIngredientStock.objects.get_or_create(
                        branch=branch,
                        ingredient=ing,
                        defaults={
                            "current_stock": stk,
                            "reserved_stock": Decimal("0.000"),
                            "min_stock_alert": min_l,
                        },
                    )
                    bis.current_stock = stk
                    bis.min_stock_alert = min_l
                    bis.save()

                return ing

            # Core ingredients with abundant capacity so stock never goes negative
            chicken_breast = seed_ingredient("Chicken Breast", "CHK-001", proteins, freezer, Ingredient.BaseUnit.GRAM, "5000", "2000", "95000", "5000", "120000")
            beef_patty = seed_ingredient("Beef Patty", "BEF-001", proteins, freezer, Ingredient.BaseUnit.GRAM, "5000", "3000", "80000", "5000", "100000")
            burger_bun = seed_ingredient("Burger Bun", "BUN-001", bakery, dry_store, Ingredient.BaseUnit.PIECE, "20", "400", "2000", "200", "2500")
            cheese = seed_ingredient("Cheese Slice", "CHS-001", dairy, refrigerator, Ingredient.BaseUnit.PIECE, "50", "750", "1800", "150", "2000")
            lettuce = seed_ingredient("Lettuce", "VEG-001", vegetables, refrigerator, Ingredient.BaseUnit.GRAM, "1000", "250", "35000", "2000", "40000")
            tomato = seed_ingredient("Tomato", "VEG-002", vegetables, refrigerator, Ingredient.BaseUnit.GRAM, "1000", "180", "40000", "2500", "50000")
            cooking_oil = seed_ingredient("Cooking Oil", "OIL-001", dry_goods, dry_store, Ingredient.BaseUnit.MILLILITRE, "5000", "850", "160000", "10000", "200000")
            pizza_dough = seed_ingredient("Pizza Dough", "PIZ-001", bakery, refrigerator, Ingredient.BaseUnit.GRAM, "5000", "700", "90000", "5000", "110000")
            mozzarella = seed_ingredient("Mozzarella Cheese", "CHS-002", dairy, refrigerator, Ingredient.BaseUnit.GRAM, "2000", "1600", "55000", "3000", "70000")
            bbq_sauce = seed_ingredient("BBQ Sauce", "SCE-001", sauces, refrigerator, Ingredient.BaseUnit.MILLILITRE, "1000", "350", "35000", "2000", "40000")
            burger_sauce = seed_ingredient("Burger Sauce", "SCE-002", sauces, refrigerator, Ingredient.BaseUnit.MILLILITRE, "1000", "300", "35000", "2000", "40000")
            mojo_bottle = seed_ingredient("Mojo 250ml", "DRK-001", beverages, beverage_store, Ingredient.BaseUnit.PIECE, "24", "720", "3500", "200", "4000")
            basmati_rice = seed_ingredient("Basmati Rice", "RIC-001", dry_goods, dry_store, Ingredient.BaseUnit.GRAM, "25000", "3500", "150000", "10000", "180000")

            # Recipes
            def seed_recipe(menu_item, ingredients, instructions):
                recipe = getattr(menu_item, "recipe", None) or Recipe.objects.filter(menu_item=menu_item).first()
                if not recipe:
                    recipe = Recipe.objects.create(
                        menu_item=menu_item, yield_quantity=Decimal("1"), instructions=instructions
                    )
                else:
                    recipe.yield_quantity = Decimal("1")
                    recipe.instructions = instructions
                    recipe.save()

                recipe.recipe_ingredients.all().delete()
                for ing, qty in ingredients:
                    RecipeIngredient.objects.create(
                        recipe=recipe, ingredient=ing, quantity=Decimal(str(qty))
                    )
                return recipe

            seed_recipe(
                chicken_burger,
                [(chicken_breast, "120"), (burger_bun, "1"), (cheese, "1"), (lettuce, "15"), (tomato, "20"), (burger_sauce, "15"), (cooking_oil, "10")],
                "Cook chicken breast, toast bun, assemble with cheese, lettuce, tomato and signature sauce."
            )
            seed_recipe(
                beef_burger,
                [(beef_patty, "130"), (burger_bun, "1"), (cheese, "1"), (lettuce, "15"), (tomato, "20"), (burger_sauce, "15")],
                "Cook beef patty, toast bun and assemble with cheese and vegetables."
            )
            seed_recipe(
                bbq_pizza,
                [(pizza_dough, "250"), (chicken_breast, "100"), (mozzarella, "120"), (bbq_sauce, "50")],
                "Prepare dough, add BBQ sauce, grilled chicken and mozzarella, bake at 250C."
            )
            seed_recipe(mojo, [(mojo_bottle, "1")], "Serve chilled bottle.")
            seed_recipe(
                chicken_biryani,
                [(basmati_rice, "180"), (chicken_breast, "150"), (cooking_oil, "20")],
                "Slow cook chicken with aromatic basmati rice and traditional spices."
            )
            seed_recipe(
                beef_tehari,
                [(basmati_rice, "180"), (beef_patty, "140"), (cooking_oil, "25")],
                "Cook mustard oil tehari with spiced tender beef chunks."
            )
            seed_recipe(
                mutton_biryani,
                [(basmati_rice, "180"), (beef_patty, "160"), (cooking_oil, "25")],
                "Fragrant kacchi biryani with slow-cooked meat and basmati rice."
            )
            seed_recipe(
                chicken_fried_rice,
                [(basmati_rice, "180"), (chicken_breast, "100"), (cooking_oil, "15")],
                "Wok fried rice with chicken, egg and scallions."
            )
            seed_recipe(
                masala_tea,
                [(cooking_oil, "5")],
                "Fresh brewed spiced milk tea."
            )
            seed_recipe(
                mango_lassi,
                [(cheese, "1")],
                "Rich blended yogurt and sweet mango puree."
            )

        # -----------------------------------------------------
        # 6. SUPPLIERS, INITIAL PURCHASES & OPENING MOVEMENTS
        # -----------------------------------------------------
        with transaction.atomic():
            def get_or_create_supplier(name, phone, contact):
                sup = Supplier.objects.filter(restaurant=restaurant, name=name).first()
                if not sup:
                    sup = Supplier.objects.create(
                        restaurant=restaurant, name=name, phone=phone, contact_name=contact
                    )
                return sup

            sup_meat = get_or_create_supplier("Bengal Meat & Poultry", "01711223344", "Hasan Ali")
            sup_agro = get_or_create_supplier("Dhaka Fresh Agro", "01711334455", "Kamal Hossain")
            sup_dry = get_or_create_supplier("Pran-RFL Foods & Beverage", "01711445566", "Rafiqul Islam")
            sup_bake = get_or_create_supplier("Bake & Flakes Supplies", "01711556677", "Nasir Ahmed")

            # Opening balance stock transactions
            opening_dt = timezone.make_aware(
                datetime.combine(start_date, time(8, 0)), tz
            )
            for branch in [dhanmondi_branch, gulshan_branch]:
                for ing in [chicken_breast, beef_patty, burger_bun, cheese, lettuce, tomato, cooking_oil, pizza_dough, mozzarella, bbq_sauce, burger_sauce, mojo_bottle, basmati_rice]:
                    StockTransaction.objects.create(
                        restaurant=restaurant,
                        branch=branch,
                        ingredient=ing,
                        transaction_type=StockTransaction.TransactionType.OPENING_BALANCE,
                        quantity=ing.current_stock,
                        unit_cost_snapshot=ing.current_unit_cost,
                        note="Initial 90-day opening stock balance",
                        created_at=opening_dt,
                    )

        # -----------------------------------------------------
        # 7. SHIFTS, ATTENDANCE, LEAVE, SALARY ADVANCES & PAYROLL
        # -----------------------------------------------------
        with transaction.atomic():
            # Approved Leave requests
            LeaveRequest.objects.create(
                restaurant=restaurant,
                branch=dhanmondi_branch,
                employee=ep_w_dhn2,
                leave_type="sick",
                start_date=today - timedelta(days=45),
                end_date=today - timedelta(days=43),
                reason="Viral fever and rest",
                status="approved",
                reviewed_by=mgr_dhn,
                reviewed_at=timezone.make_aware(datetime.combine(today - timedelta(days=46), time(12, 0)), tz),
            )
            LeaveRequest.objects.create(
                restaurant=restaurant,
                branch=gulshan_branch,
                employee=ep_w_gls1,
                leave_type="casual",
                start_date=today - timedelta(days=25),
                end_date=today - timedelta(days=24),
                reason="Family event",
                status="approved",
                reviewed_by=mgr_gls,
                reviewed_at=timezone.make_aware(datetime.combine(today - timedelta(days=26), time(11, 0)), tz),
            )

            # Salary Advances
            adv1 = SalaryAdvance.objects.create(
                restaurant=restaurant,
                branch=dhanmondi_branch,
                employee=ep_w_dhn1,
                amount=Decimal("2000.00"),
                reason="Medical emergency for family",
                status="deducted",
                reviewed_by=mgr_dhn,
                reviewed_at=timezone.make_aware(datetime.combine(today - timedelta(days=65), time(14, 0)), tz),
            )
            adv2 = SalaryAdvance.objects.create(
                restaurant=restaurant,
                branch=gulshan_branch,
                employee=ep_w_gls2,
                amount=Decimal("2500.00"),
                reason="Home renovation advance",
                status="deducted",
                reviewed_by=mgr_gls,
                reviewed_at=timezone.make_aware(datetime.combine(today - timedelta(days=35), time(15, 0)), tz),
            )

            # 90 Days Attendance records
            attendance_list = []
            for d_offset in range(91):
                cur_d = start_date + timedelta(days=d_offset)
                is_cur_today = (cur_d == today)

                for emp in all_employees:
                    # Skip if on approved leave
                    if emp == ep_w_dhn2 and (today - timedelta(days=45)) <= cur_d <= (today - timedelta(days=43)):
                        continue
                    if emp == ep_w_gls1 and (today - timedelta(days=25)) <= cur_d <= (today - timedelta(days=24)):
                        continue

                    # 1 off-day per week
                    if cur_d.weekday() == (emp.pk % 7):
                        continue

                    s_time = emp.shift.start_time if emp.shift else time(10, 0)
                    e_time = emp.shift.end_time if emp.shift else time(18, 0)

                    sched_start = timezone.make_aware(datetime.combine(cur_d, s_time), tz)
                    sched_end = timezone.make_aware(datetime.combine(cur_d, e_time), tz)

                    # Minor random variation: mostly on-time, sometimes 5-10 min late
                    late_min = rng.choice([0, 0, 0, 0, 5, 8, 12])
                    cin = sched_start + timedelta(minutes=late_min)
                    cout = sched_end + timedelta(minutes=rng.randint(5, 20)) if not is_cur_today else None

                    att = Attendance(
                        employee=emp,
                        restaurant=restaurant,
                        branch=emp.user.branch,
                        shift=emp.shift,
                        work_date=cur_d,
                        scheduled_start=sched_start,
                        scheduled_end=sched_end,
                        grace_minutes=15,
                        check_in=cin,
                        check_out=cout,
                    )
                    attendance_list.append(att)

            Attendance.objects.bulk_create(attendance_list, ignore_conflicts=True)

            # Paid Payroll for past 2 full calendar months (July & August 2026)
            # July Payroll (paid Aug 2)
            july_month = date(2026, 7, 1)
            july_paid_dt = timezone.make_aware(datetime(2026, 8, 2, 11, 0), tz)
            for emp in all_employees:
                adv_ded = Decimal("2000.00") if emp == ep_w_dhn1 else Decimal("0.00")
                net_sal = emp.basic_salary - adv_ded
                pr = PayrollRecord.objects.create(
                    employee=emp,
                    restaurant=restaurant,
                    branch=emp.user.branch,
                    month=july_month,
                    basic_salary=emp.basic_salary,
                    advance_deduction=adv_ded,
                    net_salary=net_sal,
                    status="paid",
                    paid_at=july_paid_dt,
                    note="July 2026 salary settled via bank transfer",
                )
                if emp == ep_w_dhn1:
                    adv1.payroll_record = pr
                    adv1.save()

            # August Payroll (paid Sep 2)
            aug_month = date(2026, 8, 1)
            aug_paid_dt = timezone.make_aware(datetime(2026, 9, 2, 11, 30), tz)
            for emp in all_employees:
                adv_ded = Decimal("2500.00") if emp == ep_w_gls2 else Decimal("0.00")
                net_sal = emp.basic_salary - adv_ded
                pr = PayrollRecord.objects.create(
                    employee=emp,
                    restaurant=restaurant,
                    branch=emp.user.branch,
                    month=aug_month,
                    basic_salary=emp.basic_salary,
                    advance_deduction=adv_ded,
                    net_salary=net_sal,
                    status="paid",
                    paid_at=aug_paid_dt,
                    note="August 2026 salary settled via bank transfer",
                )
                if emp == ep_w_gls2:
                    adv2.payroll_record = pr
                    adv2.save()

        self.stdout.write(
            self.style.SUCCESS(
                "✓ Shifts, Attendance (90 days), Leave, Advances & 2 Months Paid Payroll generated"
            )
        )

        # -----------------------------------------------------
        # 8. PURCHASES, INGREDIENT REQUESTS, WASTAGE & STOCK COUNTS
        # -----------------------------------------------------
        with transaction.atomic():
            # Periodic Purchase Orders received every 10-12 days per branch
            po_dates = [start_date + timedelta(days=step) for step in range(5, 86, 11)]
            for po_d in po_dates:
                for branch in [dhanmondi_branch, gulshan_branch]:
                    rec_mgr = mgr_dhn if branch == dhanmondi_branch else mgr_gls
                    po_dt = timezone.make_aware(datetime.combine(po_d, time(9, 30)), tz)

                    # Fresh Meats
                    po_meat = PurchaseOrder.objects.create(
                        restaurant=restaurant,
                        branch=branch,
                        supplier=sup_meat,
                        invoice_number=f"INV-BGL-{po_d.strftime('%m%d')}-{branch.code}",
                        purchase_date=po_d,
                        status=PurchaseOrder.Status.RECEIVED,
                        total_amount=Decimal("18000.00"),
                        received_at=po_dt,
                        received_by=rec_mgr,
                    )
                    PurchaseOrderItem.objects.create(
                        purchase_order=po_meat, ingredient=chicken_breast, pack_quantity=Decimal("6"), pack_price=Decimal("2000.00"), total_price=Decimal("12000.00")
                    )
                    PurchaseOrderItem.objects.create(
                        purchase_order=po_meat, ingredient=beef_patty, pack_quantity=Decimal("2"), pack_price=Decimal("3000.00"), total_price=Decimal("6000.00")
                    )
                    StockTransaction.objects.create(
                        restaurant=restaurant, branch=branch, ingredient=chicken_breast,
                        transaction_type=StockTransaction.TransactionType.PURCHASE,
                        quantity=Decimal("30000"), unit_cost_snapshot=chicken_breast.current_unit_cost, created_at=po_dt
                    )
                    StockTransaction.objects.create(
                        restaurant=restaurant, branch=branch, ingredient=beef_patty,
                        transaction_type=StockTransaction.TransactionType.PURCHASE,
                        quantity=Decimal("10000"), unit_cost_snapshot=beef_patty.current_unit_cost, created_at=po_dt
                    )

                    # Bakery & Dairy
                    po_bakery = PurchaseOrder.objects.create(
                        restaurant=restaurant,
                        branch=branch,
                        supplier=sup_bake,
                        invoice_number=f"INV-BKF-{po_d.strftime('%m%d')}-{branch.code}",
                        purchase_date=po_d,
                        status=PurchaseOrder.Status.RECEIVED,
                        total_amount=Decimal("12000.00"),
                        received_at=po_dt,
                        received_by=rec_mgr,
                    )
                    PurchaseOrderItem.objects.create(
                        purchase_order=po_bakery, ingredient=burger_bun, pack_quantity=Decimal("15"), pack_price=Decimal("400.00"), total_price=Decimal("6000.00")
                    )
                    PurchaseOrderItem.objects.create(
                        purchase_order=po_bakery, ingredient=pizza_dough, pack_quantity=Decimal("8"), pack_price=Decimal("700.00"), total_price=Decimal("5600.00")
                    )
                    StockTransaction.objects.create(
                        restaurant=restaurant, branch=branch, ingredient=burger_bun,
                        transaction_type=StockTransaction.TransactionType.PURCHASE,
                        quantity=Decimal("300"), unit_cost_snapshot=burger_bun.current_unit_cost, created_at=po_dt
                    )
                    StockTransaction.objects.create(
                        restaurant=restaurant, branch=branch, ingredient=pizza_dough,
                        transaction_type=StockTransaction.TransactionType.PURCHASE,
                        quantity=Decimal("40000"), unit_cost_snapshot=pizza_dough.current_unit_cost, created_at=po_dt
                    )

                    # Beverages
                    po_drk = PurchaseOrder.objects.create(
                        restaurant=restaurant,
                        branch=branch,
                        supplier=sup_dry,
                        invoice_number=f"INV-PRN-{po_d.strftime('%m%d')}-{branch.code}",
                        purchase_date=po_d,
                        status=PurchaseOrder.Status.RECEIVED,
                        total_amount=Decimal("7200.00"),
                        received_at=po_dt,
                        received_by=rec_mgr,
                    )
                    PurchaseOrderItem.objects.create(
                        purchase_order=po_drk, ingredient=mojo_bottle, pack_quantity=Decimal("10"), pack_price=Decimal("720.00"), total_price=Decimal("7200.00")
                    )
                    StockTransaction.objects.create(
                        restaurant=restaurant, branch=branch, ingredient=mojo_bottle,
                        transaction_type=StockTransaction.TransactionType.PURCHASE,
                        quantity=Decimal("240"), unit_cost_snapshot=mojo_bottle.current_unit_cost, created_at=po_dt
                    )

                    # Fresh Vegetables & Dry Agro Goods
                    po_agro = PurchaseOrder.objects.create(
                        restaurant=restaurant,
                        branch=branch,
                        supplier=sup_agro,
                        invoice_number=f"INV-DFA-{po_d.strftime('%m%d')}-{branch.code}",
                        purchase_date=po_d,
                        status=PurchaseOrder.Status.RECEIVED,
                        total_amount=Decimal("11700.00"),
                        received_at=po_dt,
                        received_by=rec_mgr,
                    )
                    PurchaseOrderItem.objects.create(
                        purchase_order=po_agro, ingredient=lettuce, pack_quantity=Decimal("15"), pack_price=Decimal("250.00"), total_price=Decimal("3750.00")
                    )
                    PurchaseOrderItem.objects.create(
                        purchase_order=po_agro, ingredient=tomato, pack_quantity=Decimal("20"), pack_price=Decimal("180.00"), total_price=Decimal("3600.00")
                    )
                    PurchaseOrderItem.objects.create(
                        purchase_order=po_agro, ingredient=basmati_rice, pack_quantity=Decimal("1"), pack_price=Decimal("3500.00"), total_price=Decimal("3500.00")
                    )
                    PurchaseOrderItem.objects.create(
                        purchase_order=po_agro, ingredient=cooking_oil, pack_quantity=Decimal("1"), pack_price=Decimal("850.00"), total_price=Decimal("850.00")
                    )
                    StockTransaction.objects.create(
                        restaurant=restaurant, branch=branch, ingredient=lettuce,
                        transaction_type=StockTransaction.TransactionType.PURCHASE,
                        quantity=Decimal("15000"), unit_cost_snapshot=lettuce.current_unit_cost, created_at=po_dt
                    )
                    StockTransaction.objects.create(
                        restaurant=restaurant, branch=branch, ingredient=tomato,
                        transaction_type=StockTransaction.TransactionType.PURCHASE,
                        quantity=Decimal("20000"), unit_cost_snapshot=tomato.current_unit_cost, created_at=po_dt
                    )
                    StockTransaction.objects.create(
                        restaurant=restaurant, branch=branch, ingredient=basmati_rice,
                        transaction_type=StockTransaction.TransactionType.PURCHASE,
                        quantity=Decimal("25000"), unit_cost_snapshot=basmati_rice.current_unit_cost, created_at=po_dt
                    )
                    StockTransaction.objects.create(
                        restaurant=restaurant, branch=branch, ingredient=cooking_oil,
                        transaction_type=StockTransaction.TransactionType.PURCHASE,
                        quantity=Decimal("5000"), unit_cost_snapshot=cooking_oil.current_unit_cost, created_at=po_dt
                    )

            # Weekly measurable wastage (spoilage, kitchen error) ~1% of COGS
            waste_dates = [start_date + timedelta(days=step) for step in range(3, 89, 7)]
            for w_d in waste_dates:
                w_dt = timezone.make_aware(datetime.combine(w_d, time(22, 0)), tz)
                for branch in [dhanmondi_branch, gulshan_branch]:
                    wr1 = WasteRecord.objects.create(
                        restaurant=restaurant, branch=branch, ingredient=lettuce,
                        quantity=Decimal("350.000"), reason=WasteRecord.Reason.SPOILAGE,
                        unit_cost_snapshot=lettuce.current_unit_cost,
                        note="Wilted outer leaves", created_at=w_dt
                    )
                    StockTransaction.objects.create(
                        restaurant=restaurant, branch=branch, ingredient=lettuce,
                        transaction_type=StockTransaction.TransactionType.WASTE,
                        quantity=Decimal("350.000"), unit_cost_snapshot=lettuce.current_unit_cost,
                        note="Spoiled lettuce", created_at=w_dt
                    )

                    wr2 = WasteRecord.objects.create(
                        restaurant=restaurant, branch=branch, ingredient=tomato,
                        quantity=Decimal("250.000"), reason=WasteRecord.Reason.KITCHEN_ERROR,
                        unit_cost_snapshot=tomato.current_unit_cost,
                        note="Bruised tomatoes discarded during prep", created_at=w_dt
                    )
                    StockTransaction.objects.create(
                        restaurant=restaurant, branch=branch, ingredient=tomato,
                        transaction_type=StockTransaction.TransactionType.WASTE,
                        quantity=Decimal("250.000"), unit_cost_snapshot=tomato.current_unit_cost,
                        note="Kitchen error tomato", created_at=w_dt
                    )

            # Monthly Physical Stock Counts
            for m_day in [today - timedelta(days=60), today - timedelta(days=30)]:
                for branch in [dhanmondi_branch, gulshan_branch]:
                    for ing in [chicken_breast, beef_patty, burger_bun, mojo_bottle]:
                        StockCount.objects.create(
                            restaurant=restaurant, branch=branch, ingredient=ing,
                            system_quantity=Decimal("4500.000"), actual_quantity=Decimal("4490.000")
                        )

            # Ingredient Requests & approval workflow
            for idx, req_d in enumerate([today - timedelta(days=70), today - timedelta(days=40), today - timedelta(days=10)]):
                req_dt = timezone.make_aware(datetime.combine(req_d, time(11, 0)), tz)
                IngredientRequest.objects.create(
                    restaurant=restaurant, branch=dhanmondi_branch, ingredient=chicken_breast,
                    quantity=Decimal("25000"), priority=IngredientRequest.Priority.HIGH,
                    status=IngredientRequest.Status.APPROVED, requested_by=chef_dhn,
                    reviewed_by=mgr_dhn, reviewed_at=req_dt, reason="Weekend stock replenishment"
                )
            # 2 Pending requests for next delivery
            IngredientRequest.objects.create(
                restaurant=restaurant, branch=dhanmondi_branch, ingredient=mozzarella,
                quantity=Decimal("10000"), priority=IngredientRequest.Priority.MEDIUM,
                status=IngredientRequest.Status.PENDING, requested_by=chef_dhn,
                reason="Upcoming weekend pizza prep"
            )
            IngredientRequest.objects.create(
                restaurant=restaurant, branch=gulshan_branch, ingredient=burger_bun,
                quantity=Decimal("100"), priority=IngredientRequest.Priority.URGENT,
                status=IngredientRequest.Status.PENDING, requested_by=chef_gls,
                reason="Low bun stock alert"
            )

        self.stdout.write(
            self.style.SUCCESS(
                "✓ Purchases, Supplier Orders, Stock Movements, Wastage & Ingredient Requests seeded"
            )
        )

        # -----------------------------------------------------
        # 9. REALISTIC 90-DAY OPERATING EXPENSES
        # -----------------------------------------------------
        with transaction.atomic():
            def get_or_create_exp_cat(name):
                ec = ExpenseCategory.objects.filter(restaurant=restaurant, name=name).first()
                return ec or ExpenseCategory.objects.create(restaurant=restaurant, name=name)

            cat_rent = get_or_create_exp_cat("Rent")
            cat_elec = get_or_create_exp_cat("Electricity & Utilities")
            cat_gas = get_or_create_exp_cat("Gas Bill")
            cat_clean = get_or_create_exp_cat("Cleaning & Sanitation Supplies")
            cat_net = get_or_create_exp_cat("Internet & POS Cloud")
            cat_mkt = get_or_create_exp_cat("Marketing & Local Promotion")

            # 3 Months of Expenses (July, August, September) at realistic rates
            for exp_month in [date(2026, 7, 5), date(2026, 8, 5), date(2026, 9, 5)]:
                # Dhanmondi Expenses
                Expense.objects.create(
                    restaurant=restaurant, branch=dhanmondi_branch, category=cat_rent,
                    amount=Decimal("24000.00"), expense_date=exp_month,
                    title=f"Monthly Rent - Dhanmondi Branch ({exp_month.strftime('%B %Y')})",
                    payment_method="BANK_TRANSFER"
                )
                Expense.objects.create(
                    restaurant=restaurant, branch=dhanmondi_branch, category=cat_elec,
                    amount=Decimal("7500.00"), expense_date=exp_month,
                    title=f"Electricity Bill - DESCO ({exp_month.strftime('%B %Y')})",
                    payment_method="BANK_TRANSFER"
                )
                Expense.objects.create(
                    restaurant=restaurant, branch=dhanmondi_branch, category=cat_gas,
                    amount=Decimal("3000.00"), expense_date=exp_month,
                    title=f"Commercial Gas Bill - Titas ({exp_month.strftime('%B %Y')})",
                    payment_method="CASH"
                )
                Expense.objects.create(
                    restaurant=restaurant, branch=dhanmondi_branch, category=cat_clean,
                    amount=Decimal("2000.00"), expense_date=exp_month,
                    title=f"Kitchen Hygiene & Sanitizer Restock", payment_method="CASH"
                )

                # Gulshan Expenses
                Expense.objects.create(
                    restaurant=restaurant, branch=gulshan_branch, category=cat_rent,
                    amount=Decimal("28000.00"), expense_date=exp_month,
                    title=f"Monthly Rent - Gulshan Branch ({exp_month.strftime('%B %Y')})",
                    payment_method="BANK_TRANSFER"
                )
                Expense.objects.create(
                    restaurant=restaurant, branch=gulshan_branch, category=cat_elec,
                    amount=Decimal("8500.00"), expense_date=exp_month,
                    title=f"Electricity Bill - DPDC ({exp_month.strftime('%B %Y')})",
                    payment_method="BANK_TRANSFER"
                )
                Expense.objects.create(
                    restaurant=restaurant, branch=gulshan_branch, category=cat_gas,
                    amount=Decimal("3500.00"), expense_date=exp_month,
                    title=f"Commercial Gas Bill - Titas ({exp_month.strftime('%B %Y')})",
                    payment_method="CASH"
                )
                Expense.objects.create(
                    restaurant=restaurant, branch=gulshan_branch, category=cat_net,
                    amount=Decimal("2000.00"), expense_date=exp_month,
                    title=f"High-Speed Fiber & Cloud POS Subscription", payment_method="CARD"
                )
                Expense.objects.create(
                    restaurant=restaurant, branch=gulshan_branch, category=cat_mkt,
                    amount=Decimal("2500.00"), expense_date=exp_month,
                    title=f"Social Media & Weekend Food Promo", payment_method="CARD"
                )

        self.stdout.write(
            self.style.SUCCESS(
                "✓ Operating Expenses seeded across 3 months for both branches"
            )
        )

        # -----------------------------------------------------
        # 10. REALISTIC 90-DAY ORDERS GENERATION
        # -----------------------------------------------------
        self.stdout.write(
            self.style.WARNING("Generating 90 days of realistic orders, sessions & payments...")
        )

        menu_items_pool = [
            (chicken_burger, Decimal("230.00")),
            (beef_burger, Decimal("250.00")),
            (bbq_pizza, Decimal("450.00")),
            (mojo, Decimal("50.00")),
            (chicken_biryani, Decimal("220.00")),
            (beef_tehari, Decimal("200.00")),
            (mutton_biryani, Decimal("350.00")),
            (chicken_fried_rice, Decimal("220.00")),
            (masala_tea, Decimal("40.00")),
            (mango_lassi, Decimal("90.00")),
        ]

        positive_comments = [
            "Food was delicious and served piping hot!",
            "Crispy chicken burger was amazing, great sauce!",
            "Best BBQ pizza in Dhanmondi, highly recommended.",
            "Prompt service and very polite waiter.",
            "Beef burger was juicy and flavorful, loved the bun.",
            "Great atmosphere and clean dining area at Gulshan branch.",
            "Fast checkout with bKash, very convenient.",
            "Excellent taste and generous portion size.",
            "Loved the mojo with fresh burger. Will visit again!",
            "Kacchi biryani meat was extremely tender and tasty.",
        ]

        total_orders_seeded = 0

        # Bulk collections
        order_items_to_create = []
        stock_txns_to_create = []
        services_to_create = []
        feedbacks_to_create = []
        payments_to_create = []

        with transaction.atomic():
            for d_idx in range(91):
                cur_date = start_date + timedelta(days=d_idx)
                is_today = (cur_date == today)
                is_weekend = (cur_date.weekday() in [4, 5])  # Friday & Saturday

                # Past days: normal operation
                # Weekdays: ~15-18 orders, Weekends: ~24-30 orders
                base_count = 26 if is_weekend else 17
                if is_today:
                    base_count = 12  # Today: 5 completed earlier + 7 live orders

                dhn_target = int(base_count * 0.58)
                gls_target = base_count - dhn_target

                branch_targets = [
                    (dhanmondi_branch, dhn_tables, dhn_waiters, dhn_target),
                    (gulshan_branch, gls_tables, gls_waiters, gls_target)
                ]

                # Specific live order roster for today to guarantee all 4 live statuses:
                # 2 NEW, 1 ACCEPTED, 2 PREPARING, 2 READY, 5 COMPLETED
                today_roster = [
                    ("COMPLETED", "DINE_IN", 12, 15, "PAID"),
                    ("COMPLETED", "TAKEAWAY", 12, 45, "PAID"),
                    ("COMPLETED", "DINE_IN", 13, 10, "PAID"),
                    ("COMPLETED", "TAKEAWAY", 13, 30, "PAID"),
                    ("COMPLETED", "DINE_IN", 14, 0, "PAID"),
                    ("READY", "DINE_IN", 14, 15, "UNPAID"),
                    ("READY", "TAKEAWAY", 14, 25, "UNPAID"),
                    ("PREPARING", "DINE_IN", 14, 30, "UNPAID"),
                    ("PREPARING", "TAKEAWAY", 14, 35, "UNPAID"),
                    ("ACCEPTED", "DINE_IN", 14, 40, "UNPAID"),
                    ("NEW", "DINE_IN", 14, 45, "UNPAID"),
                    ("NEW", "TAKEAWAY", 14, 50, "UNPAID"),
                ]
                today_idx = 0

                for branch, b_tables, b_waiters, b_count in branch_targets:
                    for ord_idx in range(b_count):
                        if not is_today:
                            # Peak selection: Lunch (40%), Dinner (50%), Afternoon (10%)
                            period_roll = rng.random()
                            if period_roll < 0.40:
                                hr = rng.randint(12, 14)
                                minute = rng.randint(0, 59)
                            elif period_roll < 0.90:
                                hr = rng.randint(19, 22)
                                minute = rng.randint(0, 59)
                            else:
                                hr = rng.randint(16, 17)
                                minute = rng.randint(0, 59)

                            order_dt = timezone.make_aware(datetime.combine(cur_date, time(hr, minute)), tz)
                            is_dine_in = (rng.random() < 0.70)
                            order_type = "DINE_IN" if is_dine_in else "TAKEAWAY"

                            roll = rng.random()
                            if roll < 0.95:
                                ord_status = "COMPLETED"
                                pay_status = "PAID"
                            elif roll < 0.98:
                                ord_status = "CANCELLED"
                                pay_status = "UNPAID"
                            else:
                                ord_status = "COMPLETED"
                                pay_status = "REFUNDED"
                        else:
                            # Today: draw from predetermined realistic live roster
                            plan = today_roster[min(today_idx, len(today_roster) - 1)]
                            today_idx += 1
                            ord_status = plan[0]
                            order_type = plan[1]
                            order_dt = timezone.make_aware(datetime.combine(cur_date, time(plan[2], plan[3])), tz)
                            pay_status = plan[4]
                            is_dine_in = (order_type == "DINE_IN")

                        table = rng.choice(b_tables) if is_dine_in else None
                        waiter_emp = rng.choice(b_waiters)

                        # TableSession handling
                        session = None
                        if is_dine_in and table:
                            # 20% same-table multiple orders support
                            if rng.random() < 0.20:
                                session = TableSession.objects.filter(
                                    table=table, status=TableSession.STATUS_OPEN
                                ).first()

                            if not session:
                                session = TableSession.objects.create(
                                    restaurant=restaurant,
                                    branch=branch,
                                    table=table,
                                    status=TableSession.STATUS_OPEN,
                                    opened_at=order_dt,
                                )

                        # Choose realistic items and quantities (ticket size ~৳750-1,200)
                        item_count = rng.choice([2, 2, 3, 3, 4])
                        item_sample = rng.sample(menu_items_pool, k=min(item_count, len(menu_items_pool)))
                        order_item_configs = []
                        for m_it, price in item_sample:
                            qty = rng.choice([1, 1, 2])
                            order_item_configs.append((m_it, price, qty))
                        subtotal = sum(p * q for _, p, q in order_item_configs)

                        pm = rng.choice(["CASH", "CASH", "MOBILE_BANKING", "MOBILE_BANKING", "CARD"])
                        order = Order.objects.create(
                            restaurant=restaurant,
                            branch=branch,
                            table=table,
                            table_session=session,
                            order_type=order_type,
                            status=ord_status,
                            subtotal=subtotal,
                            discount_amount=Decimal("0.00"),
                            service_charge=Decimal("0.00"),
                            vat_amount=Decimal("0.00"),
                            total_amount=subtotal,
                            payment_status=pay_status,
                            payment_method=pm if pay_status in ["PAID", "REFUNDED"] else "",
                        )

                        # Prep durations & stage timestamps
                        prep_mins = rng.randint(12, 22)
                        ready_dt = order_dt + timedelta(minutes=prep_mins)
                        served_dt = ready_dt + timedelta(minutes=rng.randint(2, 5))
                        completed_dt = served_dt + timedelta(minutes=rng.randint(15, 30))

                        status_changed = None
                        if ord_status in ["READY", "SERVED", "COMPLETED"]:
                            status_changed = ready_dt
                        elif ord_status == "PREPARING":
                            status_changed = order_dt + timedelta(minutes=5)
                        elif ord_status == "ACCEPTED":
                            status_changed = order_dt + timedelta(minutes=2)

                        paid_dt_val = completed_dt if pay_status in ["PAID", "REFUNDED"] else None
                        ref_amt = subtotal if pay_status == "REFUNDED" else Decimal("0.00")

                        Order.objects.filter(pk=order.pk).update(
                            created_at=order_dt,
                            status_changed_at=status_changed,
                            paid_at=paid_dt_val,
                            refund_amount=ref_amt,
                        )

                        # OrderItems & COGS recipe consumption
                        for m_it, price, qty in order_item_configs:
                            oi = OrderItem(
                                order=order,
                                menu_item=m_it,
                                quantity=qty,
                                price=price,
                                discount_amount=Decimal("0.00"),
                            )
                            order_items_to_create.append(oi)

                            if ord_status in ["READY", "SERVED", "COMPLETED"]:
                                rec = getattr(m_it, "recipe", None)
                                if rec:
                                    for ri in rec.recipe_ingredients.all():
                                        stx = StockTransaction(
                                            restaurant=restaurant,
                                            branch=branch,
                                            ingredient=ri.ingredient,
                                            transaction_type=StockTransaction.TransactionType.CONSUMPTION,
                                            quantity=ri.quantity * qty,
                                            order=order,
                                            unit_cost_snapshot=ri.ingredient.current_unit_cost,
                                            created_at=order_dt,
                                        )
                                        stock_txns_to_create.append(stx)

                        # Payments
                        if pay_status == "PAID":
                            pt = PaymentTransaction(
                                restaurant=restaurant,
                                branch=branch,
                                order=order,
                                transaction_type=PaymentTransaction.TYPE_PAYMENT,
                                amount=subtotal,
                                payment_method=pm,
                                transaction_at=paid_dt_val,
                            )
                            payments_to_create.append(pt)
                        elif pay_status == "REFUNDED":
                            pt_pay = PaymentTransaction(
                                restaurant=restaurant,
                                branch=branch,
                                order=order,
                                transaction_type=PaymentTransaction.TYPE_PAYMENT,
                                amount=subtotal,
                                payment_method=pm,
                                transaction_at=paid_dt_val,
                            )
                            pt_ref = PaymentTransaction(
                                restaurant=restaurant,
                                branch=branch,
                                order=order,
                                transaction_type=PaymentTransaction.TYPE_REFUND,
                                amount=subtotal,
                                payment_method=pm,
                                transaction_at=paid_dt_val + timedelta(minutes=5),
                            )
                            payments_to_create.extend([pt_pay, pt_ref])

                        # Service Performance tracking
                        svc = OrderStaffService(
                            order=order,
                            waiter=waiter_emp,
                            order_taken_by=waiter_emp,
                            served_by=waiter_emp if ord_status in ["SERVED", "COMPLETED"] else None,
                            ready_at=ready_dt if ord_status in ["READY", "SERVED", "COMPLETED"] else None,
                            served_at=served_dt if ord_status in ["SERVED", "COMPLETED"] else None,
                            completed_at=completed_dt if ord_status == "COMPLETED" else None,
                        )
                        services_to_create.append(svc)

                        # Customer feedback
                        if ord_status == "COMPLETED" and rng.random() < 0.28:
                            fb = OrderFeedback(
                                order=order,
                                rating=rng.choice([4, 5, 5, 5, 4, 3]),
                                comment=rng.choice(positive_comments),
                                submitted_at=completed_dt + timedelta(minutes=rng.randint(5, 20)),
                            )
                            feedbacks_to_create.append(fb)

                        total_orders_seeded += 1

            # Bulk creation for performance
            OrderItem.objects.bulk_create(order_items_to_create)
            StockTransaction.objects.bulk_create(stock_txns_to_create)
            PaymentTransaction.objects.bulk_create(payments_to_create)
            OrderStaffService.objects.bulk_create(services_to_create)
            OrderFeedback.objects.bulk_create(feedbacks_to_create)

            # Synchronize BranchIngredientStock and Ingredient current stock with transaction ledgers
            for b in [dhanmondi_branch, gulshan_branch]:
                for ing in Ingredient.objects.filter(restaurant=restaurant):
                    b_txns = StockTransaction.objects.filter(branch=b, ingredient=ing)
                    qty_in = b_txns.filter(
                        transaction_type__in=[
                            StockTransaction.TransactionType.OPENING_BALANCE,
                            StockTransaction.TransactionType.PURCHASE,
                            StockTransaction.TransactionType.ADJUSTMENT_IN,
                        ]
                    ).aggregate(total=Sum("quantity"))["total"] or Decimal("0")
                    qty_out = b_txns.filter(
                        transaction_type__in=[
                            StockTransaction.TransactionType.CONSUMPTION,
                            StockTransaction.TransactionType.WASTE,
                            StockTransaction.TransactionType.ADJUSTMENT_OUT,
                        ]
                    ).aggregate(total=Sum("quantity"))["total"] or Decimal("0")
                    net_stk = max(Decimal("0.000"), qty_in - qty_out)
                    BranchIngredientStock.objects.filter(branch=b, ingredient=ing).update(current_stock=net_stk)

            for ing in Ingredient.objects.filter(restaurant=restaurant):
                tot_stk = BranchIngredientStock.objects.filter(ingredient=ing).aggregate(s=Sum("current_stock"))["s"] or Decimal("0.000")
                Ingredient.objects.filter(pk=ing.pk).update(current_stock=tot_stk)

            # Assign service assignment times
            for svc in OrderStaffService.objects.filter(order__restaurant=restaurant):
                OrderStaffService.objects.filter(pk=svc.pk).update(assigned_at=svc.order.created_at)

            # Close past completed TableSessions, keep today's active ones open
            TableSession.objects.filter(
                restaurant=restaurant,
                orders__status="COMPLETED"
            ).exclude(
                orders__status__in=["NEW", "ACCEPTED", "PREPARING", "READY"]
            ).update(status=TableSession.STATUS_CLOSED, closed_at=timezone.now())

            # Mark tables with live orders as OCCUPIED
            active_live_tables = Order.objects.filter(
                restaurant=restaurant,
                status__in=["NEW", "ACCEPTED", "PREPARING", "READY"],
                table__isnull=False
            ).values_list("table_id", flat=True)
            Table.objects.filter(pk__in=active_live_tables).update(status=Table.STATUS_OCCUPIED)

            # -----------------------------------------------------
            # 11. RESERVATIONS
            # -----------------------------------------------------
            res_names = [
                ("Tanvir Rahman", "01711998877"),
                ("Sumaiya Islam", "01711887766"),
                ("Imtiaz Ahmed", "01711776655"),
                ("Dr. Mahbubul Alam", "01711665544"),
                ("Sabrina Yasmin", "01711554433"),
                ("Kamrul Hassan", "01711443322"),
                ("Tahmina Chowdhury", "01711332211"),
                ("Engr. Shahriar", "01711221100"),
                ("Farhana Haque", "01711112233"),
                ("Nazmul Huda", "01711223399"),
                ("Shahnaz Begum", "01711334488"),
                ("Saiful Islam", "01711445577"),
            ]
            for r_idx, (r_name, r_phone) in enumerate(res_names):
                b = dhanmondi_branch if r_idx % 2 == 0 else gulshan_branch
                if r_idx < 8:
                    r_date = today - timedelta(days=r_idx * 9 + 4)
                    r_status = Reservation.Status.COMPLETED
                elif r_idx < 10:
                    # Upcoming reservations: tonight and tomorrow!
                    r_date = today + timedelta(days=(r_idx - 8))
                    r_status = Reservation.Status.CONFIRMED
                else:
                    r_date = today - timedelta(days=18)
                    r_status = Reservation.Status.CANCELLED

                Reservation.objects.create(
                    restaurant=restaurant,
                    branch=b,
                    name=r_name,
                    phone=r_phone,
                    date=r_date,
                    time=time(19, 30),
                    party_size=rng.choice([2, 4, 6, 8]),
                    note="Window booth preferred, celebration dinner" if r_idx % 2 == 0 else "",
                    status=r_status,
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"✓ Successfully seeded {total_orders_seeded} total orders over 90 days"
            )
        )

        # -----------------------------------------------------
        # 12. FINAL COUNTS, FINANCIAL RECONCILIATION & REPORT
        # -----------------------------------------------------
        self._report_final_summary(restaurant, dhanmondi_branch, gulshan_branch, start_date, today, tz)

    def _report_final_summary(self, restaurant, dhanmondi_branch, gulshan_branch, start_date, today, tz):
        total_branches = restaurant.branches.count()
        total_categories = Category.objects.filter(restaurant=restaurant).count()
        total_menu_items = MenuItem.objects.filter(category__restaurant=restaurant).count()
        total_ing_cats = IngredientCategory.objects.filter(restaurant=restaurant).count()
        total_locations = StorageLocation.objects.filter(restaurant=restaurant).count()
        total_ingredients = Ingredient.objects.filter(restaurant=restaurant).count()
        total_recipes = Recipe.objects.filter(menu_item__category__restaurant=restaurant).count()
        total_suppliers = Supplier.objects.filter(restaurant=restaurant).count()
        total_pos = PurchaseOrder.objects.filter(restaurant=restaurant).count()
        total_stock_txns = StockTransaction.objects.filter(ingredient__restaurant=restaurant).count()
        total_waste = WasteRecord.objects.filter(ingredient__restaurant=restaurant).count()
        total_users = User.objects.filter(restaurant=restaurant).count()
        total_employees = EmployeeProfile.objects.filter(user__restaurant=restaurant).count()
        total_attendance = Attendance.objects.filter(restaurant=restaurant).count()
        total_payroll = PayrollRecord.objects.filter(restaurant=restaurant).count()
        total_orders = Order.objects.filter(restaurant=restaurant).count()
        total_sessions = TableSession.objects.filter(restaurant=restaurant).count()
        total_feedbacks = OrderFeedback.objects.filter(order__restaurant=restaurant).count()
        total_reservations = Reservation.objects.filter(restaurant=restaurant).count()

        # Today's live active orders
        live_orders_qs = Order.objects.filter(
            restaurant=restaurant,
            created_at__date=today,
            status__in=["NEW", "ACCEPTED", "PREPARING", "READY"],
        ).order_by("created_at")

        # 90-day Financial Metrics via official P&L service
        start_dt = timezone.make_aware(datetime.combine(start_date, time.min), tz)
        end_dt = timezone.make_aware(datetime.combine(today, time.max), tz)

        pnl = get_profit_and_loss_data(
            restaurant=restaurant,
            start_date=start_date,
            end_date=today,
            start_datetime=start_dt,
            end_datetime=end_dt,
        )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=" * 70))
        self.stdout.write(self.style.SUCCESS("     RESTAURANT 360 - 90-DAY REALISTIC DEMO DATA SUMMARY"))
        self.stdout.write(self.style.SUCCESS("=" * 70))

        self.stdout.write(self.style.SUCCESS("\n[1. ENTITY & DATABASE COUNTS]"))
        self.stdout.write(f"  Target Restaurant:           {restaurant.name} (ID: {restaurant.pk})")
        self.stdout.write(f"  Branches ({total_branches}):                {dhanmondi_branch.name} (Main), {gulshan_branch.name}")
        self.stdout.write(f"  Tables:                      {Table.objects.filter(restaurant=restaurant).count()} tables")
        self.stdout.write(f"  Staff Members:               {total_employees} employees ({total_users} user accounts)")
        self.stdout.write(f"  Attendance Records:          {total_attendance} records over 90 days")
        self.stdout.write(f"  Payroll Records:             {total_payroll} records (July & August settled)")
        self.stdout.write(f"  Menu Categories:             {total_categories}")
        self.stdout.write(f"  Menu Items:                  {total_menu_items}")
        self.stdout.write(f"  Active Recipes:              {total_recipes}")
        self.stdout.write(f"  Ingredients:                 {total_ingredients} items (in {total_locations} storage locations)")
        self.stdout.write(f"  Suppliers:                   {total_suppliers}")
        self.stdout.write(f"  Received Purchase Orders:    {total_pos}")
        self.stdout.write(f"  Stock Transactions:          {total_stock_txns}")
        self.stdout.write(f"  Measurable Waste Records:    {total_waste}")
        self.stdout.write(f"  Total Orders:                {total_orders} across 90 days")
        self.stdout.write(f"  Table Sessions:              {total_sessions}")
        self.stdout.write(f"  Customer Feedbacks:          {total_feedbacks} ratings & comments")
        self.stdout.write(f"  Table Reservations:          {total_reservations}")

        self.stdout.write(self.style.SUCCESS("\n[2. 90-DAY PROFIT & LOSS FINANCIAL PERFORMANCE]"))
        self.stdout.write(f"  Report Period:               {start_date.strftime('%b %d, %Y')} - {today.strftime('%b %d, %Y')} (90 Days)")
        self.stdout.write(f"  Net Sales (Food Revenue):    ৳{pnl['net_sales']:,.2f}")
        self.stdout.write(f"  Gross Collections:           ৳{pnl['gross_collections']:,.2f}")
        self.stdout.write(f"  Refunds:                     ৳{pnl['refunds']:,.2f}")
        self.stdout.write(f"  COGS (Recipe Consumption):   ৳{pnl['cogs']:,.2f}")
        self.stdout.write(f"  Measurable Waste Loss:       ৳{pnl['waste_loss']:,.2f}")
        self.stdout.write(f"  Gross Profit:                ৳{pnl['gross_profit']:,.2f} ({pnl['gross_margin_percent']}%)")
        self.stdout.write(f"  Paid Staff Payroll:          ৳{pnl['payroll_cost']:,.2f}")
        self.stdout.write(f"  General Operating Expenses:  ৳{pnl['operating_expenses']:,.2f}")
        self.stdout.write(f"  Operating Profit (EBIT):     ৳{pnl['operating_profit']:,.2f} ({pnl['operating_margin_percent']}%)")
        self.stdout.write(f"  Settled Paid Orders Count:   {pnl['paid_orders_count']}")

        self.stdout.write(self.style.SUCCESS(f"\n[3. TODAY'S LIVE ACTIVE ORDERS ({today.strftime('%b %d, %Y')})]"))
        if live_orders_qs.exists():
            for o in live_orders_qs:
                tbl_info = f"Table #{o.table.table_number}" if o.table else "Takeaway"
                branch_name = o.branch.name if o.branch else "Main"
                self.stdout.write(
                    f"  Order #{o.id:04d} | Status: {o.status:<9} | {o.order_type:<8} | {branch_name:<16} | {tbl_info:<10} | Amount: ৳{o.total_amount}"
                )
        else:
            self.stdout.write("  No live orders currently pending.")

        self.stdout.write(self.style.SUCCESS("\n" + "=" * 70 + "\n"))