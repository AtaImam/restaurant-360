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
]