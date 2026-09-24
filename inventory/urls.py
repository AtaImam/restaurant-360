from django.urls import path

from . import views


urlpatterns = [
    # Ingredient Groups
    path(
        "categories/",
        views.inventory_category_list,
        name="inventory_category_list",
    ),
    path(
        "categories/add/",
        views.inventory_category_create,
        name="inventory_category_create",
    ),
    path(
        "categories/<int:category_id>/edit/",
        views.inventory_category_edit,
        name="inventory_category_edit",
    ),
    path(
        "categories/<int:category_id>/delete/",
        views.inventory_category_delete,
        name="inventory_category_delete",
    ),

    # Inventory Items
    path(
        "",
        views.ingredient_list,
        name="ingredient_list",
    ),
    path(
        "items/add/",
        views.ingredient_create,
        name="ingredient_create",
    ),
    path(
        "items/<int:ingredient_id>/",
        views.ingredient_detail,
        name="ingredient_detail",
    ),
    path(
        "items/<int:ingredient_id>/edit/",
        views.ingredient_edit,
        name="ingredient_edit",
    ),
    path(
        "items/<int:ingredient_id>/toggle-active/",
        views.ingredient_toggle_active,
        name="ingredient_toggle_active",
    ),

    # Suppliers
    path("suppliers/", views.supplier_list, name="supplier_list"),
    path("suppliers/add/", views.supplier_create, name="supplier_create"),
    path("suppliers/<int:supplier_id>/edit/", views.supplier_edit, name="supplier_edit"),
    path("suppliers/<int:supplier_id>/toggle/", views.supplier_toggle, name="supplier_toggle"),

    # Purchases
    path("purchases/", views.purchase_list, name="purchase_list"),
    path("purchases/add/", views.purchase_create, name="purchase_create"),
    path("purchases/<int:purchase_id>/", views.purchase_detail, name="purchase_detail"),
    path("purchases/<int:purchase_id>/receive/", views.purchase_receive, name="purchase_receive"),

    # Wastage
    path("waste/", views.waste_list, name="waste_list"),
    path("waste/add/", views.waste_create, name="waste_create"),

    # Stock Counts / Audits
    path("counts/", views.stock_count_list, name="stock_count_list"),
    path("counts/add/", views.stock_count_create, name="stock_count_create"),

    # Stock Transactions Ledger
    path("transactions/", views.stock_transaction_list, name="stock_transaction_list"),

    # Ingredient Requests (Owner / Manager)
    path("requests/", views.ingredient_request_list, name="ingredient_request_list"),
    path("requests/<int:request_id>/approve/", views.approve_ingredient_request, name="approve_ingredient_request"),
    path("requests/<int:request_id>/reject/", views.reject_ingredient_request, name="reject_ingredient_request"),
    path("requests/<int:request_id>/convert-to-po/", views.convert_request_to_po, name="convert_request_to_po"),
]