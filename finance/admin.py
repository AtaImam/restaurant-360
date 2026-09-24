from django.contrib import admin
from .models import Expense, ExpenseCategory


@admin.register(ExpenseCategory)
class ExpenseCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "restaurant", "is_active", "created_at")
    list_filter = ("restaurant", "is_active")
    search_fields = ("name", "description")


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ("title", "restaurant", "category", "amount", "expense_date", "payment_method")
    list_filter = ("restaurant", "category", "payment_method", "expense_date")
    search_fields = ("title", "payee", "notes")
