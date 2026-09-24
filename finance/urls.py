from django.urls import path
from . import reports_views, views

app_name = "finance"

urlpatterns = [
    # Expenses
    path("expenses/", views.expense_list, name="expense_list"),
    path("expenses/add/", views.expense_create, name="expense_create"),
    path("expenses/<int:expense_id>/edit/", views.expense_edit, name="expense_edit"),
    path("expenses/<int:expense_id>/delete/", views.expense_delete, name="expense_delete"),

    # Categories
    path("categories/", views.category_list, name="category_list"),
    path("categories/add/", views.category_create, name="category_create"),
    path("categories/<int:category_id>/edit/", views.category_edit, name="category_edit"),
    path("categories/<int:category_id>/delete/", views.category_delete, name="category_delete"),

    # Refund
    path("orders/<int:order_id>/refund/", views.record_refund_view, name="record_refund"),

    # Reports
    path("reports/sales/", reports_views.sales_report, name="sales_report"),
    path("reports/item-sales/", reports_views.item_sales_report, name="item_sales_report"),
    path("reports/stock/", reports_views.stock_report, name="stock_report"),
    path("reports/purchases/", reports_views.purchase_report, name="purchase_report"),
    path("reports/profit-loss/", reports_views.profit_loss_report, name="profit_loss_report"),
]
