from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from restaurant.models import Restaurant
from staff.models import EmployeeProfile, Shift


User = get_user_model()


DEMO_PASSWORD = "Demo@12345"


DEMO_EMPLOYEES = [
    {
        "username": "demo_manager_01",
        "first_name": "Arif",
        "last_name": "Rahman",
        "email": "arif.manager@demo.local",
        "phone": "01710000001",
        "role": "manager",
        "employee_id": "DEMO-001",
        "basic_salary": Decimal("45000.00"),
    },
    {
        "username": "demo_waiter_01",
        "first_name": "Nabil",
        "last_name": "Hasan",
        "email": "nabil.waiter@demo.local",
        "phone": "01710000002",
        "role": "waiter",
        "employee_id": "DEMO-002",
        "basic_salary": Decimal("18000.00"),
    },
    {
        "username": "demo_waiter_02",
        "first_name": "Sadia",
        "last_name": "Akter",
        "email": "sadia.waiter@demo.local",
        "phone": "01710000003",
        "role": "waiter",
        "employee_id": "DEMO-003",
        "basic_salary": Decimal("18500.00"),
    },
    {
        "username": "demo_waiter_03",
        "first_name": "Rakib",
        "last_name": "Hossain",
        "email": "rakib.waiter@demo.local",
        "phone": "01710000004",
        "role": "waiter",
        "employee_id": "DEMO-004",
        "basic_salary": Decimal("17500.00"),
    },
    {
        "username": "demo_waiter_04",
        "first_name": "Mim",
        "last_name": "Islam",
        "email": "mim.waiter@demo.local",
        "phone": "01710000005",
        "role": "waiter",
        "employee_id": "DEMO-005",
        "basic_salary": Decimal("19000.00"),
    },
    {
        "username": "demo_waiter_05",
        "first_name": "Tanvir",
        "last_name": "Ahmed",
        "email": "tanvir.waiter@demo.local",
        "phone": "01710000006",
        "role": "waiter",
        "employee_id": "DEMO-006",
        "basic_salary": Decimal("18000.00"),
    },
    {
        "username": "demo_chef_01",
        "first_name": "Shakil",
        "last_name": "Mia",
        "email": "shakil.chef@demo.local",
        "phone": "01710000007",
        "role": "chief",
        "employee_id": "DEMO-007",
        "basic_salary": Decimal("30000.00"),
    },
    {
        "username": "demo_chef_02",
        "first_name": "Nusrat",
        "last_name": "Jahan",
        "email": "nusrat.chef@demo.local",
        "phone": "01710000008",
        "role": "chief",
        "employee_id": "DEMO-008",
        "basic_salary": Decimal("32000.00"),
    },
    {
        "username": "demo_kitchen_manager_01",
        "first_name": "Mahmud",
        "last_name": "Karim",
        "email": "mahmud.kitchen@demo.local",
        "phone": "01710000009",
        "role": "kitchen_manager",
        "employee_id": "DEMO-009",
        "basic_salary": Decimal("40000.00"),
    },
    {
        "username": "demo_bar_manager_01",
        "first_name": "Farhan",
        "last_name": "Kabir",
        "email": "farhan.bar@demo.local",
        "phone": "01710000010",
        "role": "bar_manager",
        "employee_id": "DEMO-010",
        "basic_salary": Decimal("35000.00"),
    },
]


class Command(BaseCommand):
    help = (
        "Create or update reusable demo staff accounts "
        "for a restaurant."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--restaurant-id",
            type=int,
            help=(
                "Restaurant ID for demo employees. "
                "The first restaurant is used if omitted."
            ),
        )

    @transaction.atomic
    def handle(self, *args, **options):
        restaurant_id = options.get("restaurant_id")

        restaurants = Restaurant.objects.order_by("pk")

        if restaurant_id:
            restaurant = restaurants.filter(
                pk=restaurant_id
            ).first()

            if restaurant is None:
                raise CommandError(
                    f"Restaurant ID {restaurant_id} was not found."
                )
        else:
            restaurant = restaurants.first()

            if restaurant is None:
                raise CommandError(
                    "No restaurant exists. Create a restaurant first."
                )

        shifts = list(
            Shift.objects.filter(
                restaurant=restaurant,
                is_active=True,
            ).order_by("start_time", "pk")
        )

        if not shifts:
            raise CommandError(
                "No active shift exists for this restaurant. "
                "Create at least one shift first."
            )

        created_users = 0
        updated_users = 0

        self.stdout.write(
            f"Restaurant: {restaurant.name} (ID: {restaurant.pk})"
        )

        for index, employee_data in enumerate(DEMO_EMPLOYEES):
            user, user_created = User.objects.get_or_create(
                username=employee_data["username"],
            )

            user.first_name = employee_data["first_name"]
            user.last_name = employee_data["last_name"]
            user.email = employee_data["email"]
            user.phone = employee_data["phone"]
            user.role = employee_data["role"]
            user.restaurant = restaurant
            user.is_active = True
            user.is_active_staff = True
            user.is_staff = False
            user.is_superuser = False
            user.set_password(DEMO_PASSWORD)
            user.save()

            assigned_shift = shifts[index % len(shifts)]

            EmployeeProfile.objects.update_or_create(
                user=user,
                defaults={
                    "employee_id": employee_data["employee_id"],
                    "joining_date": date(2026, 1, 1),
                    "shift": assigned_shift,
                    "address": "Demo address, Dhaka",
                    "emergency_contact_name": "Demo Contact",
                    "emergency_contact_phone": "01810000000",
                    "basic_salary": employee_data["basic_salary"],
                },
            )

            if user_created:
                created_users += 1
                result = "created"
            else:
                updated_users += 1
                result = "updated"

            self.stdout.write(
                f"  {employee_data['employee_id']} | "
                f"{employee_data['username']} | "
                f"{employee_data['role']} | "
                f"{assigned_shift.name} | {result}"
            )

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Demo staff complete: {created_users} created, "
                f"{updated_users} updated."
            )
        )
        self.stdout.write(
            self.style.WARNING(
                f"Demo password for every account: {DEMO_PASSWORD}"
            )
        )