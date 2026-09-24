from decimal import Decimal
from django.db import migrations


def backfill_main_branch(apps, schema_editor):
    Restaurant = apps.get_model("restaurant", "Restaurant")
    Branch = apps.get_model("restaurant", "Branch")
    Floor = apps.get_model("restaurant", "Floor")
    Table = apps.get_model("restaurant", "Table")
    User = apps.get_model("users", "User")
    StorageLocation = apps.get_model("inventory", "StorageLocation")
    Ingredient = apps.get_model("inventory", "Ingredient")
    BranchIngredientStock = apps.get_model("inventory", "BranchIngredientStock")
    StockTransaction = apps.get_model("inventory", "StockTransaction")
    StockReservation = apps.get_model("inventory", "StockReservation")
    WasteRecord = apps.get_model("inventory", "WasteRecord")
    StockCount = apps.get_model("inventory", "StockCount")
    PurchaseOrder = apps.get_model("inventory", "PurchaseOrder")
    TableSession = apps.get_model("orders", "TableSession")
    Order = apps.get_model("orders", "Order")
    PaymentTransaction = apps.get_model("orders", "PaymentTransaction")
    Shift = apps.get_model("staff", "Shift")
    Attendance = apps.get_model("staff", "Attendance")
    LeaveRequest = apps.get_model("staff", "LeaveRequest")
    SalaryAdvance = apps.get_model("staff", "SalaryAdvance")
    PayrollRecord = apps.get_model("staff", "PayrollRecord")
    DailyTableAssignment = apps.get_model("staff", "DailyTableAssignment")
    StaffTask = apps.get_model("staff", "StaffTask")
    StaffNotification = apps.get_model("staff", "StaffNotification")
    Expense = apps.get_model("finance", "Expense")

    for restaurant in Restaurant.objects.all():
        branch, created = Branch.objects.get_or_create(
            restaurant=restaurant,
            is_main=True,
            defaults={
                "name": "Main Branch",
                "code": "MAIN-01",
                "address": restaurant.address or "",
                "phone": restaurant.phone or "",
                "is_active": True,
            },
        )

        Floor.objects.filter(restaurant=restaurant, branch__isnull=True).update(branch=branch)
        Table.objects.filter(restaurant=restaurant, branch__isnull=True).update(branch=branch)
        User.objects.filter(restaurant=restaurant, branch__isnull=True).update(branch=branch)
        StorageLocation.objects.filter(restaurant=restaurant, branch__isnull=True).update(branch=branch)
        TableSession.objects.filter(restaurant=restaurant, branch__isnull=True).update(branch=branch)
        Order.objects.filter(restaurant=restaurant, branch__isnull=True).update(branch=branch)
        PaymentTransaction.objects.filter(restaurant=restaurant, branch__isnull=True).update(branch=branch)
        Shift.objects.filter(restaurant=restaurant, branch__isnull=True).update(branch=branch)
        Attendance.objects.filter(restaurant=restaurant, branch__isnull=True).update(branch=branch)
        LeaveRequest.objects.filter(restaurant=restaurant, branch__isnull=True).update(branch=branch)
        SalaryAdvance.objects.filter(restaurant=restaurant, branch__isnull=True).update(branch=branch)
        PayrollRecord.objects.filter(restaurant=restaurant, branch__isnull=True).update(branch=branch)
        DailyTableAssignment.objects.filter(restaurant=restaurant, branch__isnull=True).update(branch=branch)
        StaffTask.objects.filter(restaurant=restaurant, branch__isnull=True).update(branch=branch)
        StaffNotification.objects.filter(restaurant=restaurant, branch__isnull=True).update(branch=branch)
        Expense.objects.filter(restaurant=restaurant, branch__isnull=True).update(branch=branch)
        PurchaseOrder.objects.filter(restaurant=restaurant, branch__isnull=True).update(branch=branch)
        StockTransaction.objects.filter(restaurant=restaurant, branch__isnull=True).update(branch=branch)
        StockReservation.objects.filter(order__restaurant=restaurant, branch__isnull=True).update(branch=branch)
        WasteRecord.objects.filter(ingredient__restaurant=restaurant, branch__isnull=True).update(
            restaurant=restaurant, branch=branch
        )
        StockCount.objects.filter(ingredient__restaurant=restaurant, branch__isnull=True).update(
            restaurant=restaurant, branch=branch
        )

        for ingredient in Ingredient.objects.filter(restaurant=restaurant):
            BranchIngredientStock.objects.get_or_create(
                branch=branch,
                ingredient=ingredient,
                defaults={
                    "current_stock": ingredient.current_stock or Decimal("0.000"),
                    "reserved_stock": Decimal("0.000"),
                    "min_stock_alert": ingredient.minimum_level or Decimal("0.000"),
                },
            )


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("restaurant", "0005_branch_remove_floor_unique_floor_name_per_restaurant_and_more"),
        ("users", "0002_user_branch"),
        ("inventory", "0008_purchaseorder_branch_stockcount_branch_and_more"),
        ("orders", "0010_order_branch_paymenttransaction_branch_and_more"),
        ("staff", "0008_remove_shift_staff_unique_shift_name_per_restaurant_and_more"),
        ("finance", "0002_expense_branch"),
    ]

    operations = [
        migrations.RunPython(backfill_main_branch, reverse_noop),
    ]
