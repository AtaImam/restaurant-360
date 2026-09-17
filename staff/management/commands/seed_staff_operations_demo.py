from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from restaurant.models import Restaurant, Table
from staff.models import (
    Attendance,
    DailyTableAssignment,
    EmployeeProfile,
    StaffNotification,
    StaffTask,
)
from staff.services import ATTENDANCE_TIMEZONE


User = get_user_model()


class Command(BaseCommand):
    help = (
        "Create or update reusable Daily Operations demo data "
        "without changing real employee attendance."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--restaurant-id",
            type=int,
            help=(
                "Restaurant ID for operations demo data. "
                "The first restaurant is used if omitted."
            ),
        )

    def handle(self, *args, **options):
        restaurant_id = options.get("restaurant_id")

        restaurants = Restaurant.objects.order_by("pk")

        if restaurant_id:
            restaurant = restaurants.filter(pk=restaurant_id).first()
        else:
            restaurant = restaurants.first()

        if restaurant is None:
            raise CommandError("No restaurant was found.")

        actor = (
            User.objects.filter(is_active=True)
            .filter(
                Q(is_superuser=True)
                | Q(role__in=["admin", "owner", "manager"])
            )
            .filter(
                Q(is_superuser=True)
                | Q(restaurant_id=restaurant.pk)
            )
            .order_by("-is_superuser", "pk")
            .first()
        )

        if actor is None:
            raise CommandError(
                "No active Owner, Admin or Manager account was found."
            )

        demo_employees = list(
            EmployeeProfile.objects.filter(
                employee_id__startswith="DEMO-",
                user__restaurant_id=restaurant.pk,
                user__is_active=True,
                user__is_active_staff=True,
            )
            .select_related("user", "shift")
            .order_by("employee_id")
        )

        if not demo_employees:
            raise CommandError(
                "No DEMO employees were found. Run seed_staff_demo first."
            )

        now = timezone.localtime(
            timezone.now(),
            ATTENDANCE_TIMEZONE,
        )
        work_date = now.date()

        attendance_created = 0
        attendance_updated = 0
        attendance_records = {}

        with transaction.atomic():
            for index, employee in enumerate(demo_employees):
                shift = employee.shift

                if shift is None or not shift.is_active:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipped {employee.employee_id}: "
                            "no active shift assigned."
                        )
                    )
                    continue

                scheduled_start = timezone.make_aware(
                    datetime.combine(
                        work_date,
                        shift.start_time,
                    ),
                    ATTENDANCE_TIMEZONE,
                )

                scheduled_end = timezone.make_aware(
                    datetime.combine(
                        work_date,
                        shift.end_time,
                    ),
                    ATTENDANCE_TIMEZONE,
                )

                if scheduled_end <= scheduled_start:
                    scheduled_end += timedelta(days=1)

                demo_delay = [0, 4, 8, 12, 3][index % 5]
                planned_check_in = scheduled_start + timedelta(
                    minutes=demo_delay
                )

                # Keep demo employees visibly present even if their
                # scheduled shift starts later than the current time.
                check_in = min(now, planned_check_in)

                attendance, created = Attendance.objects.update_or_create(
                    employee=employee,
                    work_date=work_date,
                    defaults={
                        "restaurant": restaurant,
                        "shift": shift,
                        "scheduled_start": scheduled_start,
                        "scheduled_end": scheduled_end,
                        "grace_minutes": shift.grace_minutes,
                        "check_in": check_in,
                        "check_out": None,
                    },
                )

                attendance_records[employee.pk] = attendance

                if created:
                    attendance_created += 1
                else:
                    attendance_updated += 1

            # Ensure at least six reusable restaurant tables exist.
            demo_tables = []

            for table_number in range(1, 7):
                table, _ = Table.objects.get_or_create(
                    restaurant=restaurant,
                    table_number=table_number,
                    defaults={"is_active": True},
                )

                if not table.is_active:
                    table.is_active = True
                    table.save(update_fields=["is_active"])

                demo_tables.append(table)

            demo_waiters = [
                employee
                for employee in demo_employees
                if employee.user.role == "waiter"
                and employee.pk in attendance_records
            ]

            assignments_created = 0
            assignments_existing = 0

            if demo_waiters:
                for index, table in enumerate(demo_tables):
                    existing_assignment = (
                        DailyTableAssignment.objects.filter(
                            table=table,
                            work_date=work_date,
                            is_active=True,
                        )
                        .select_related("waiter")
                        .first()
                    )

                    if existing_assignment is not None:
                        assignments_existing += 1
                        continue

                    waiter = demo_waiters[index % len(demo_waiters)]
                    attendance = attendance_records[waiter.pk]

                    assignment = DailyTableAssignment.objects.create(
                        work_date=work_date,
                        restaurant=restaurant,
                        table=table,
                        waiter=waiter,
                        attendance=attendance,
                        assigned_by=actor,
                        is_active=True,
                    )

                    assignments_created += 1

                    StaffNotification.objects.get_or_create(
                        recipient=waiter.user,
                        event_key=(
                            f"demo-table-assignment-"
                            f"{work_date}-{assignment.pk}"
                        ),
                        defaults={
                            "restaurant": restaurant,
                            "notification_type": "task_assigned",
                            "title": "Table assignment",
                            "message": (
                                f"You are assigned to Table "
                                f"{table.table_number} today."
                            ),
                        },
                    )

            task_plans = {
                "manager": [
                    (
                        "Opening floor inspection",
                        "Check dining floor readiness and coordinate staff.",
                    ),
                ],
                "waiter": [
                    (
                        "Prepare assigned tables",
                        "Check table cleanliness, menus and serving items.",
                    ),
                ],
                "chief": [
                    (
                        "Review kitchen preparation",
                        "Check ingredients and coordinate today’s food prep.",
                    ),
                ],
                "kitchen_manager": [
                    (
                        "Kitchen station inspection",
                        "Confirm stations, hygiene and preparation readiness.",
                    ),
                ],
                "bar_manager": [
                    (
                        "Prepare beverage station",
                        "Check beverage stock and prepare the service counter.",
                    ),
                ],
            }

            tasks_created = 0
            tasks_existing = 0

            for employee in demo_employees:
                attendance = attendance_records.get(employee.pk)

                if attendance is None:
                    continue

                plans = task_plans.get(employee.user.role, [])

                for title, instructions in plans:
                    task, created = StaffTask.objects.get_or_create(
                        restaurant=restaurant,
                        work_date=work_date,
                        employee=employee,
                        title=title,
                        defaults={
                            "attendance": attendance,
                            "instructions": instructions,
                            "assigned_by": actor,
                            "is_completed": False,
                        },
                    )

                    if created:
                        tasks_created += 1
                    else:
                        tasks_existing += 1

                    StaffNotification.objects.get_or_create(
                        recipient=employee.user,
                        event_key=f"demo-task-{work_date}-{task.pk}",
                        defaults={
                            "restaurant": restaurant,
                            "notification_type": "task_assigned",
                            "title": "New task assigned",
                            "message": title,
                        },
                    )

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Daily Operations demo ready for {work_date}"
            )
        )
        self.stdout.write(
            f"Restaurant: {restaurant} (ID: {restaurant.pk})"
        )
        self.stdout.write(
            f"Attendance: {attendance_created} created, "
            f"{attendance_updated} updated"
        )
        self.stdout.write(
            f"Table assignments: {assignments_created} created, "
            f"{assignments_existing} preserved"
        )
        self.stdout.write(
            f"Tasks: {tasks_created} created, "
            f"{tasks_existing} preserved"
        )
        self.stdout.write(
            "Existing real employee attendance and existing active "
            "table assignments were not replaced."
        )