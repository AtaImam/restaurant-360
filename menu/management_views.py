from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render

from inventory.models import Ingredient, Recipe, RecipeIngredient
from menu.models import Category, MenuItem


def calculate_menu_item_metrics(menu_item):
    recipe_cost = Decimal("0.00")
    food_cost_percentage = Decimal("0.00")
    gross_profit = menu_item.price
    possible_portions = 0
    has_recipe = False

    try:
        recipe = menu_item.recipe
        has_recipe = True

    except Recipe.DoesNotExist:
        return {
            "has_recipe": False,
            "recipe_cost": recipe_cost,
            "food_cost_percentage": food_cost_percentage,
            "gross_profit": gross_profit,
            "possible_portions": possible_portions,
        }

    recipe_ingredients = (
        recipe
        .recipe_ingredients
        .select_related(
            "ingredient"
        )
        .all()
    )

    portion_values = []

    for recipe_ingredient in recipe_ingredients:
        ingredient = recipe_ingredient.ingredient

        quantity = recipe_ingredient.quantity

        ingredient_cost = (
            quantity
            * ingredient.current_unit_cost
        )

        recipe_cost += ingredient_cost

        if quantity > 0:
            portions = int(
                ingredient.available_stock
                / quantity
            )

            portion_values.append(
                portions
            )

    if portion_values:
        possible_portions = min(
            portion_values
        )

    if menu_item.price > 0:
        food_cost_percentage = (
            recipe_cost
            / menu_item.price
        ) * Decimal("100")

    gross_profit = (
        menu_item.price
        - recipe_cost
    )

    return {
        "has_recipe": has_recipe,
        "recipe_cost": recipe_cost,
        "food_cost_percentage": food_cost_percentage,
        "gross_profit": gross_profit,
        "possible_portions": possible_portions,
    }


def menu_item_list(request):
    menu_items = (
        MenuItem.objects
        .select_related(
            "category",
            "category__restaurant",
        )
        .order_by(
            "category__restaurant__name",
            "category__name",
            "name",
        )
    )

    rows = []

    available_count = 0
    unavailable_count = 0
    recipe_count = 0

    for item in menu_items:
        if item.is_available:
            available_count += 1
        else:
            unavailable_count += 1

        metrics = calculate_menu_item_metrics(
            item
        )

        if metrics["has_recipe"]:
            recipe_count += 1

        rows.append(
            {
                "item": item,
                "has_recipe": metrics[
                    "has_recipe"
                ],
                "recipe_cost": metrics[
                    "recipe_cost"
                ],
                "food_cost_percentage": metrics[
                    "food_cost_percentage"
                ],
                "gross_profit": metrics[
                    "gross_profit"
                ],
                "possible_portions": metrics[
                    "possible_portions"
                ],
            }
        )

    context = {
        "rows": rows,
        "total_items": len(rows),
        "available_count": available_count,
        "unavailable_count": unavailable_count,
        "recipe_count": recipe_count,
    }

    return render(
        request,
        "dashboard/menu_items.html",
        context,
    )


def menu_item_create(request):
    categories = (
        Category.objects
        .select_related(
            "restaurant"
        )
        .order_by(
            "restaurant__name",
            "name",
        )
    )

    error_message = None

    if request.method == "POST":
        name = request.POST.get(
            "name",
            "",
        ).strip()

        description = request.POST.get(
            "description",
            "",
        ).strip()

        category_id = request.POST.get(
            "category"
        )

        price_text = request.POST.get(
            "price",
            "",
        ).strip()

        is_available = (
            request.POST.get(
                "is_available"
            )
            == "on"
        )

        image = request.FILES.get(
            "image"
        )

        price = Decimal("0")

        if not name:
            error_message = (
                "Menu item name is required."
            )

        elif not category_id:
            error_message = (
                "Please select a menu category."
            )

        elif not price_text:
            error_message = (
                "Selling price is required."
            )

        else:
            try:
                price = Decimal(
                    price_text
                )

                if price < 0:
                    error_message = (
                        "Price cannot be negative."
                    )

            except InvalidOperation:
                error_message = (
                    "Please enter a valid price."
                )

        category = None

        if (
            error_message is None
            and category_id
        ):
            try:
                category = (
                    Category.objects.get(
                        id=category_id
                    )
                )

            except Category.DoesNotExist:
                error_message = (
                    "Selected category does not exist."
                )

        if error_message is None:
            menu_item = MenuItem.objects.create(
                category=category,
                name=name,
                description=description,
                price=price,
                image=image,
                is_available=is_available,
            )

            return redirect(
                "recipe_builder",
                item_id=menu_item.id,
            )

    context = {
        "categories": categories,
        "error_message": error_message,

        "form_name": request.POST.get(
            "name",
            "",
        ),

        "form_description": request.POST.get(
            "description",
            "",
        ),

        "form_price": request.POST.get(
            "price",
            "",
        ),

        "selected_category": request.POST.get(
            "category",
            "",
        ),

        "form_available": (
            request.POST.get(
                "is_available"
            )
            == "on"
            if request.method == "POST"
            else True
        ),
    }

    return render(
        request,
        "dashboard/menu_item_form.html",
        context,
    )


@transaction.atomic
def recipe_builder(
    request,
    item_id,
):
    menu_item = get_object_or_404(
        MenuItem.objects.select_related(
            "category",
            "category__restaurant",
        ),
        id=item_id,
    )

    restaurant = (
        menu_item
        .category
        .restaurant
    )

    ingredients = (
        Ingredient.objects
        .select_related(
            "category",
            "restaurant",
        )
        .filter(
            restaurant=restaurant,
            is_active=True,
        )
        .order_by(
            "name"
        )
    )

    error_message = None

    existing_rows = []

    try:
        existing_recipe = (
            menu_item.recipe
        )

        existing_recipe_ingredients = (
            existing_recipe
            .recipe_ingredients
            .select_related(
                "ingredient"
            )
            .all()
        )

        for row in existing_recipe_ingredients:
            existing_rows.append(
                {
                    "ingredient_id":
                        row.ingredient_id,

                    "quantity":
                        row.quantity,
                }
            )

    except Recipe.DoesNotExist:
        existing_recipe = None

    if request.method == "POST":
        ingredient_ids = (
            request.POST.getlist(
                "ingredient_id"
            )
        )

        quantities = (
            request.POST.getlist(
                "quantity"
            )
        )

        recipe_data = {}

        for (
            ingredient_id,
            quantity_text,
        ) in zip(
            ingredient_ids,
            quantities,
        ):
            ingredient_id = (
                ingredient_id.strip()
            )

            quantity_text = (
                quantity_text.strip()
            )

            if (
                not ingredient_id
                and not quantity_text
            ):
                continue

            if not ingredient_id:
                error_message = (
                    "Please select an ingredient "
                    "for every recipe row."
                )

                break

            try:
                quantity = Decimal(
                    quantity_text
                )

            except InvalidOperation:
                error_message = (
                    "Please enter a valid "
                    "ingredient quantity."
                )

                break

            if quantity <= 0:
                error_message = (
                    "Ingredient quantity must "
                    "be greater than zero."
                )

                break

            try:
                ingredient = (
                    Ingredient.objects.get(
                        id=ingredient_id,
                        restaurant=restaurant,
                        is_active=True,
                    )
                )

            except Ingredient.DoesNotExist:
                error_message = (
                    "One of the selected "
                    "ingredients is invalid."
                )

                break

            if ingredient.id in recipe_data:
                recipe_data[
                    ingredient.id
                ] += quantity

            else:
                recipe_data[
                    ingredient.id
                ] = quantity

        if (
            error_message is None
            and not recipe_data
        ):
            error_message = (
                "Please add at least "
                "one ingredient."
            )

        if error_message is None:
            recipe, created = (
                Recipe.objects.get_or_create(
                    menu_item=menu_item,

                    defaults={
                        "yield_quantity":
                            Decimal("1"),
                    },
                )
            )

            recipe.yield_quantity = (
                Decimal("1")
            )

            recipe.instructions = (
                request.POST.get(
                    "instructions",
                    "",
                ).strip()
            )

            recipe.save()

            recipe.recipe_ingredients.all().delete()

            for (
                ingredient_id,
                quantity,
            ) in recipe_data.items():

                ingredient = (
                    Ingredient.objects.get(
                        id=ingredient_id
                    )
                )

                RecipeIngredient.objects.create(
                    recipe=recipe,
                    ingredient=ingredient,
                    quantity=quantity,
                )

            return redirect(
                "menu_item_list"
            )

        existing_rows = []

        for (
            ingredient_id,
            quantity,
        ) in zip(
            ingredient_ids,
            quantities,
        ):
            if (
                ingredient_id
                or quantity
            ):
                existing_rows.append(
                    {
                        "ingredient_id":
                            ingredient_id,

                        "quantity":
                            quantity,
                    }
                )

    if not existing_rows:
        existing_rows = [
            {
                "ingredient_id": "",
                "quantity": "",
            }
        ]

    context = {
        "menu_item": menu_item,
        "restaurant": restaurant,
        "ingredients": ingredients,
        "recipe_rows": existing_rows,
        "error_message": error_message,

        "instructions": (
            request.POST.get(
                "instructions",
                "",
            )
            if request.method == "POST"

            else (
                existing_recipe.instructions
                if existing_recipe
                else ""
            )
        ),
    }

    return render(
        request,
        "dashboard/recipe_builder.html",
        context,
    )