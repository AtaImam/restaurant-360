from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render

from inventory.models import Ingredient, Recipe, RecipeIngredient
from inventory.services import get_menu_item_requirements
from menu.models import Category, MenuItem
from users.decorators import manager_required


def calculate_menu_item_metrics(menu_item):
    try:
        requirements = get_menu_item_requirements(menu_item)
    except ValidationError:
        requirements = {}

    recipe_cost = sum(
        (row["required_quantity"] * row["ingredient"].current_unit_cost
         for row in requirements.values()),
        Decimal("0.00"),
    )
    possible_portions = min(
        (int(row["ingredient"].available_stock / row["required_quantity"])
         if row["ingredient"].is_active else 0
         for row in requirements.values()),
        default=0,
    )
    return {
        "has_recipe": bool(requirements),
        "recipe_cost": recipe_cost,
        "food_cost_percentage": (
            recipe_cost / menu_item.price * Decimal("100")
            if menu_item.price > 0 else Decimal("0.00")
        ),
        "gross_profit": menu_item.price - recipe_cost,
        "possible_portions": possible_portions,
    }


@manager_required
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

    if request.user.restaurant_id:
        menu_items = menu_items.filter(category__restaurant_id=request.user.restaurant_id)

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


@manager_required
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

    if request.user.restaurant_id:
        categories = categories.filter(restaurant_id=request.user.restaurant_id)

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

                if not price.is_finite() or price < 0 or price > Decimal("99999999.99"):
                    error_message = (
                        "Price must be between 0 and 99999999.99."
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
                    categories.get(id=category_id)
                )

            except (Category.DoesNotExist, ValueError, TypeError):
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


@manager_required
@transaction.atomic
def recipe_builder(request, item_id):
    menu_items = MenuItem.objects.select_related("category", "category__restaurant")
    if request.user.restaurant_id:
        menu_items = menu_items.filter(category__restaurant_id=request.user.restaurant_id)
    if request.method == "POST":
        menu_items = menu_items.select_for_update()
    menu_item = get_object_or_404(menu_items, id=item_id)
    restaurant = menu_item.category.restaurant

    # Sets derive requirements from their component foods. Never create or
    # overwrite a direct ingredient recipe for a set, including a crafted POST.
    if menu_item.is_set_menu:
        return render(request, "dashboard/recipe_builder.html", {
            "menu_item": menu_item,
            "restaurant": restaurant,
            "set_components": menu_item.set_components.select_related("component"),
            "error_message": (
                "Set menus use their component foods' recipes; direct ingredient recipes are not allowed."
                if request.method == "POST" else None
            ),
        }, status=400 if request.method == "POST" else 200)

    ingredients = Ingredient.objects.filter(
        restaurant=restaurant, is_active=True,
    ).select_related("category", "restaurant").order_by("name")
    existing_recipe = Recipe.objects.filter(menu_item=menu_item).first()
    existing_rows = list(existing_recipe.recipe_ingredients.values(
        "ingredient_id", "quantity"
    )) if existing_recipe else []
    error_message = None

    if request.method == "POST":
        ingredient_ids = request.POST.getlist("ingredient_id")
        quantities = request.POST.getlist("quantity")
        recipe_data = {}
        try:
            if len(ingredient_ids) != len(quantities):
                raise ValidationError("Each ingredient needs a quantity.")
            for ingredient_id, quantity_text in zip(ingredient_ids, quantities):
                ingredient_id, quantity_text = ingredient_id.strip(), quantity_text.strip()
                if not ingredient_id and not quantity_text:
                    continue
                if not ingredient_id:
                    raise ValidationError("Please select an ingredient for every recipe row.")
                try:
                    quantity = Decimal(quantity_text)
                    ingredient = ingredients.get(id=ingredient_id)
                except (InvalidOperation, ValueError, TypeError, Ingredient.DoesNotExist):
                    raise ValidationError("Please select an existing ingredient and enter a valid quantity.")
                if not quantity.is_finite() or quantity <= 0:
                    raise ValidationError("Ingredient quantities must be finite and greater than zero.")
                recipe_data[ingredient.id] = recipe_data.get(ingredient.id, Decimal("0")) + quantity

            if not recipe_data:
                raise ValidationError("Please add at least one ingredient.")
            quantity_field = RecipeIngredient._meta.get_field("quantity")
            for quantity in recipe_data.values():
                quantity_field.clean(quantity, None)
        except ValidationError as error:
            error_message = " ".join(error.messages)

        if error_message is None:
            recipe, _ = Recipe.objects.get_or_create(
                menu_item=menu_item, defaults={"yield_quantity": Decimal("1")},
            )
            # Preserve existing batch yield and customer-visible ingredient flags.
            recipe.instructions = request.POST.get("instructions", "").strip()
            recipe.save(update_fields=["instructions", "updated_at"])
            for ingredient_id, quantity in recipe_data.items():
                RecipeIngredient.objects.update_or_create(
                    recipe=recipe,
                    ingredient_id=ingredient_id,
                    defaults={"quantity": quantity},
                )
            recipe.recipe_ingredients.exclude(ingredient_id__in=recipe_data).delete()
            return redirect("menu_item_list")

        existing_rows = [
            {"ingredient_id": ingredient_id, "quantity": quantity}
            for ingredient_id, quantity in zip(ingredient_ids, quantities)
            if ingredient_id or quantity
        ]

    return render(request, "dashboard/recipe_builder.html", {
        "menu_item": menu_item,
        "restaurant": restaurant,
        "ingredients": ingredients,
        "recipe_rows": existing_rows or [{"ingredient_id": "", "quantity": ""}],
        "error_message": error_message,
        "instructions": (
            request.POST.get("instructions", "") if request.method == "POST"
            else existing_recipe.instructions if existing_recipe else ""
        ),
        "recipe_yield": existing_recipe.yield_quantity if existing_recipe else Decimal("1"),
    }, status=400 if error_message else 200)
