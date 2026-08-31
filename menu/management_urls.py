from django.urls import path

from . import management_views


urlpatterns = [
    path(
        "items/",
        management_views.menu_item_list,
        name="menu_item_list",
    ),

    path(
        "items/add/",
        management_views.menu_item_create,
        name="menu_item_create",
    ),

    path(
        "items/<int:item_id>/recipe/",
        management_views.recipe_builder,
        name="recipe_builder",
    ),
]