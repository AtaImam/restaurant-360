from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from inventory.models import (
    AddonOptionIngredient,
    Ingredient,
    Recipe,
    RecipeIngredient,
)
from inventory.services import get_menu_item_requirements
from menu.models import AddonGroup, AddonOption, Category, MenuItem, SetMenuComponent
from restaurant.models import Restaurant
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

        item_type = request.POST.get("item_type", MenuItem.ItemType.NORMAL).strip().upper()
        if item_type not in dict(MenuItem.ItemType.choices):
            item_type = MenuItem.ItemType.NORMAL

        serves_text = request.POST.get("serves", "1").strip()
        try:
            serves = max(int(serves_text), 1)
        except ValueError:
            serves = 1

        if error_message is None:
            menu_item = MenuItem.objects.create(
                category=category,
                name=name,
                description=description,
                price=price,
                image=image,
                is_available=is_available,
                item_type=item_type,
                serves=serves,
            )

            addon_group_ids = request.POST.getlist("addon_groups")
            if addon_group_ids:
                menu_item.addon_groups.set(
                    AddonGroup.objects.filter(
                        id__in=addon_group_ids,
                        restaurant=category.restaurant,
                    )
                )

            messages.success(request, f"Menu item '{menu_item.name}' created.")
            return redirect(
                "recipe_builder",
                item_id=menu_item.id,
            )

    active_rest_id = request.user.restaurant_id or (categories.first().restaurant_id if categories.exists() else None)
    context = {
        "categories": categories,
        "available_addon_groups": AddonGroup.objects.filter(
            restaurant_id=active_rest_id,
            is_active=True,
        ).order_by("display_order", "name"),
        "selected_addon_group_ids": [int(i) for i in request.POST.getlist("addon_groups") if i.isdigit()],
        "error_message": error_message,
        "page_title": "Add Menu Item",
        "button_text": "Create Menu Item",
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
        "form_item_type": request.POST.get("item_type", MenuItem.ItemType.NORMAL),
        "form_serves": request.POST.get("serves", "1"),
        "item_type_choices": MenuItem.ItemType.choices,
    }

    return render(
        request,
        "dashboard/menu_item_form.html",
        context,
    )


@manager_required
def menu_item_edit(request, item_id):
    menu_items = MenuItem.objects.select_related("category", "category__restaurant")
    if request.user.restaurant_id:
        menu_items = menu_items.filter(category__restaurant_id=request.user.restaurant_id)
    item = get_object_or_404(menu_items, id=item_id)

    categories = (
        Category.objects
        .select_related("restaurant")
        .order_by("restaurant__name", "name")
    )
    if request.user.restaurant_id:
        categories = categories.filter(restaurant_id=request.user.restaurant_id)

    error_message = None

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()
        category_id = request.POST.get("category")
        price_text = request.POST.get("price", "").strip()
        is_available = request.POST.get("is_available") == "on"
        item_type = request.POST.get("item_type", item.item_type).strip().upper()
        serves_text = request.POST.get("serves", str(item.serves)).strip()
        image = request.FILES.get("image")

        price = Decimal("0")
        serves = 1

        if not name:
            error_message = "Menu item name is required."
        elif not category_id:
            error_message = "Please select a menu category."
        elif not price_text:
            error_message = "Selling price is required."
        else:
            try:
                price = Decimal(price_text)
                if not price.is_finite() or price < 0 or price > Decimal("99999999.99"):
                    error_message = "Price must be between 0 and 99999999.99."
            except InvalidOperation:
                error_message = "Please enter a valid price."

            try:
                serves = max(int(serves_text), 1)
            except ValueError:
                serves = 1

        category = None
        if error_message is None and category_id:
            try:
                category = categories.get(id=category_id)
            except (Category.DoesNotExist, ValueError, TypeError):
                error_message = "Selected category does not exist."

        if error_message is None:
            item.name = name
            item.description = description
            item.category = category
            item.price = price
            item.is_available = is_available
            if item_type in dict(MenuItem.ItemType.choices):
                item.item_type = item_type
            item.serves = serves
            if image:
                item.image = image
            item.save()

            addon_group_ids = request.POST.getlist("addon_groups")
            item.addon_groups.set(
                AddonGroup.objects.filter(
                    id__in=addon_group_ids,
                    restaurant=category.restaurant,
                )
            )

            messages.success(request, f"Updated '{item.name}' successfully.")
            return redirect("menu_item_list")

    context = {
        "item": item,
        "categories": categories,
        "available_addon_groups": AddonGroup.objects.filter(
            restaurant=item.category.restaurant,
            is_active=True,
        ).order_by("display_order", "name"),
        "selected_addon_group_ids": (
            [int(i) for i in request.POST.getlist("addon_groups") if i.isdigit()]
            if request.method == "POST"
            else list(item.addon_groups.values_list("id", flat=True))
        ),
        "error_message": error_message,
        "page_title": f"Edit {item.name}",
        "button_text": "Save Changes",
        "form_name": request.POST.get("name", item.name),
        "form_description": request.POST.get("description", item.description),
        "form_price": request.POST.get("price", str(item.price)),
        "selected_category": str(request.POST.get("category", item.category_id)),
        "form_available": (request.POST.get("is_available") == "on") if request.method == "POST" else item.is_available,
        "form_item_type": request.POST.get("item_type", item.item_type),
        "form_serves": request.POST.get("serves", str(item.serves)),
        "item_type_choices": MenuItem.ItemType.choices,
    }
    return render(request, "dashboard/menu_item_form.html", context)


@manager_required
@require_POST
def menu_item_toggle_availability(request, item_id):
    menu_items = MenuItem.objects.all()
    if request.user.restaurant_id:
        menu_items = menu_items.filter(category__restaurant_id=request.user.restaurant_id)
    item = get_object_or_404(menu_items, id=item_id)
    item.is_available = not item.is_available
    item.save(update_fields=["is_available"])
    status_label = "Available" if item.is_available else "Unavailable"
    messages.success(request, f"'{item.name}' is now marked as {status_label}.")
    return redirect("menu_item_list")


@manager_required
@require_POST
def menu_item_delete(request, item_id):
    menu_items = MenuItem.objects.all()
    if request.user.restaurant_id:
        menu_items = menu_items.filter(category__restaurant_id=request.user.restaurant_id)
    item = get_object_or_404(menu_items, id=item_id)
    name = item.name
    try:
        item.delete()
        messages.success(request, f"Deleted menu item '{name}'.")
    except ProtectedError:
        messages.error(
            request,
            f"Cannot delete '{name}' because it is linked to orders, recipes, or set menus. Try disabling it instead.",
        )
    return redirect("menu_item_list")


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

    # Sets derive requirements from their component foods.
    if menu_item.is_set_menu:
        available_components = MenuItem.objects.filter(
            category__restaurant=restaurant,
            item_type=MenuItem.ItemType.NORMAL,
        ).exclude(id=menu_item.id).order_by("name")

        error_message = None
        if request.method == "POST":
            if "ingredient_id" in request.POST:
                error_message = "direct ingredient recipes are not allowed"
            else:
                component_ids = request.POST.getlist("component_id")
                quantities = request.POST.getlist("quantity")
                new_components = {}
                try:
                    if len(component_ids) != len(quantities):
                        raise ValidationError("Each component needs a quantity.")

                    for comp_id, qty_text in zip(component_ids, quantities):
                        comp_id = comp_id.strip()
                        qty_text = qty_text.strip()
                        if not comp_id and not qty_text:
                            continue
                        if not comp_id:
                            raise ValidationError("Please select a food item for each component row.")
                        try:
                            qty = Decimal(qty_text)
                            comp = available_components.get(id=comp_id)
                        except (InvalidOperation, ValueError, TypeError, MenuItem.DoesNotExist):
                            raise ValidationError("Select an existing normal food item and enter a valid quantity.")
                        if qty <= 0 or not qty.is_finite():
                            raise ValidationError("Component quantities must be greater than zero.")
                        new_components[comp.id] = qty

                    if not new_components:
                        raise ValidationError("A Set Menu must contain at least one component food item.")

                    with transaction.atomic():
                        menu_item.set_components.exclude(component_id__in=new_components.keys()).delete()
                        for idx, (comp_id, qty) in enumerate(new_components.items()):
                            SetMenuComponent.objects.update_or_create(
                                set_menu=menu_item,
                                component_id=comp_id,
                                defaults={"quantity": qty, "display_order": idx},
                            )
                    messages.success(request, f"Set Menu components for '{menu_item.name}' updated successfully.")
                    return redirect("menu_item_list")
                except ValidationError as err:
                    error_message = " ".join(err.messages)

        set_components = menu_item.set_components.select_related("component").order_by("display_order", "id")
        component_rows = [
            {"component_id": c.component_id, "quantity": c.quantity}
            for c in set_components
        ]
        if not component_rows:
            component_rows = [{"component_id": "", "quantity": Decimal("1.00")}]

        return render(
            request,
            "dashboard/recipe_builder.html",
            {
                "menu_item": menu_item,
                "restaurant": restaurant,
                "available_components": available_components,
                "set_components": set_components,
                "component_rows": component_rows,
                "error_message": error_message,
            },
            status=400 if error_message else 200,
        )

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


# ============================================================
# ADDON GROUPS & OPTIONS MANAGEMENT
# ============================================================

def _get_active_restaurant(request):
    if hasattr(request, "user") and request.user.is_authenticated:
        if getattr(request.user, "restaurant", None):
            return request.user.restaurant
    return Restaurant.objects.first()


@manager_required
def addon_group_list(request):
    restaurant = _get_active_restaurant(request)
    groups = (
        AddonGroup.objects.filter(restaurant=restaurant)
        .prefetch_related("options", "menu_items")
        .order_by("display_order", "id")
    )

    q = request.GET.get("q", "").strip()
    if q:
        groups = groups.filter(name__icontains=q)

    status_filter = request.GET.get("status", "").strip().lower()
    if status_filter == "active":
        groups = groups.filter(is_active=True)
    elif status_filter == "inactive":
        groups = groups.filter(is_active=False)

    return render(
        request,
        "menu/addon_group_list.html",
        {
            "groups": groups,
            "restaurant": restaurant,
            "q": q,
            "status_filter": status_filter,
            "total_groups": groups.count(),
        },
    )


@manager_required
def addon_group_create(request):
    restaurant = _get_active_restaurant(request)
    available_menu_items = (
        MenuItem.objects.filter(category__restaurant=restaurant)
        .select_related("category")
        .order_by("category__name", "name")
    )
    available_ingredients = (
        Ingredient.objects.filter(restaurant=restaurant, is_active=True)
        .select_related("category")
        .order_by("category__name", "name")
    )

    error_message = None

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        selection_type = request.POST.get("selection_type", AddonGroup.SelectionType.SINGLE).strip().upper()
        is_required = request.POST.get("is_required") in ("on", "1", "true", "True")
        min_selection_text = request.POST.get("min_selection", "0").strip()
        max_selection_text = request.POST.get("max_selection", "").strip()
        is_active = request.POST.get("is_active") in ("on", "1", "true", "True")
        display_order_text = request.POST.get("display_order", "0").strip()
        menu_item_ids = request.POST.getlist("menu_items")

        try:
            min_selection = max(0, int(min_selection_text or 0))
        except ValueError:
            min_selection = 0

        max_selection = None
        if max_selection_text:
            try:
                max_selection = max(1, int(max_selection_text))
            except ValueError:
                max_selection = None

        try:
            display_order = max(0, int(display_order_text or 0))
        except ValueError:
            display_order = 0

        option_keys = request.POST.getlist("option_key[]")
        option_ids = request.POST.getlist("option_id[]")
        option_names = request.POST.getlist("option_name[]")
        option_prices = request.POST.getlist("option_price[]")
        option_actives = request.POST.getlist("option_active[]")

        options_data = []
        try:
            for idx, (opt_name, opt_price, opt_act) in enumerate(zip(option_names, option_prices, option_actives)):
                opt_name = opt_name.strip()
                if not opt_name:
                    continue
                try:
                    p = Decimal(opt_price.strip() or "0.00")
                    if p < 0:
                        p = Decimal("0.00")
                except (InvalidOperation, ValueError):
                    p = Decimal("0.00")
                is_act = opt_act in ("1", "true", "True", "on")

                key = option_keys[idx] if idx < len(option_keys) else (option_ids[idx] if idx < len(option_ids) else f"opt_{idx}")
                ing_ids = request.POST.getlist(f"option_ingredient_{key}[]")
                ing_qtys = request.POST.getlist(f"option_quantity_{key}[]")

                ingredients_map = {}
                for ing_id, ing_qty in zip(ing_ids, ing_qtys):
                    ing_id = ing_id.strip()
                    ing_qty = ing_qty.strip()
                    if not ing_id and not ing_qty:
                        continue
                    if not ing_id:
                        raise ValidationError(f"Please select an ingredient for option '{opt_name}'.")
                    try:
                        qty = Decimal(ing_qty)
                        if qty <= 0 or not qty.is_finite():
                            raise ValidationError(f"Ingredient quantity must be greater than zero for option '{opt_name}'.")
                        ing_obj = available_ingredients.get(id=int(ing_id))
                        ingredients_map[ing_obj.id] = ingredients_map.get(ing_obj.id, Decimal("0")) + qty
                    except (InvalidOperation, ValueError, TypeError, Ingredient.DoesNotExist):
                        raise ValidationError(f"Select a valid active ingredient for option '{opt_name}'.")

                options_data.append({
                    "key": key,
                    "name": opt_name,
                    "price": p,
                    "is_active": is_act,
                    "display_order": idx,
                    "ingredients": ingredients_map,
                })
        except ValidationError as val_err:
            error_message = " ".join(val_err.messages) if hasattr(val_err, "messages") else str(val_err)

        if error_message is None:
            if not name:
                error_message = "Addon group name is required."
            elif not options_data:
                error_message = "Please add at least one option to this addon group."
            else:
                try:
                    with transaction.atomic():
                        group = AddonGroup(
                            restaurant=restaurant,
                            name=name,
                            selection_type=selection_type,
                            is_required=is_required,
                            min_selection=min_selection,
                            max_selection=max_selection,
                            is_active=is_active,
                            display_order=display_order,
                        )
                        group.full_clean()
                        group.save()

                        if menu_item_ids:
                            valid_items = MenuItem.objects.filter(
                                id__in=menu_item_ids,
                                category__restaurant=restaurant,
                            )
                            group.menu_items.set(valid_items)

                        for opt in options_data:
                            addon_opt = AddonOption.objects.create(
                                group=group,
                                name=opt["name"],
                                price=opt["price"],
                                is_active=opt["is_active"],
                                display_order=opt["display_order"],
                            )
                            for ing_id, qty in opt["ingredients"].items():
                                AddonOptionIngredient.objects.create(
                                    addon_option=addon_opt,
                                    ingredient_id=ing_id,
                                    quantity=qty,
                                )

                    messages.success(request, f"Addon group '{group.name}' created successfully.")
                    return redirect("addon_group_list")
                except ValidationError as e:
                    error_message = " ".join(e.messages) if hasattr(e, "messages") else str(e)

    option_rows = []
    if request.method == "POST":
        option_keys = request.POST.getlist("option_key[]")
        option_ids = request.POST.getlist("option_id[]")
        option_names = request.POST.getlist("option_name[]")
        option_prices = request.POST.getlist("option_price[]")
        option_actives = request.POST.getlist("option_active[]")
        for idx, (opt_name, opt_price, opt_act) in enumerate(zip(option_names, option_prices, option_actives)):
            key = option_keys[idx] if idx < len(option_keys) else (option_ids[idx] if idx < len(option_ids) else f"opt_{idx}")
            ing_ids = request.POST.getlist(f"option_ingredient_{key}[]")
            ing_qtys = request.POST.getlist(f"option_quantity_{key}[]")
            option_rows.append({
                "key": key,
                "id": option_ids[idx] if idx < len(option_ids) else "",
                "name": opt_name,
                "price": opt_price,
                "is_active": opt_act in ("1", "true", "True", "on"),
                "ingredients": [
                    {"ingredient_id": int(i_id) if i_id.isdigit() else "", "quantity": i_qty}
                    for i_id, i_qty in zip(ing_ids, ing_qtys)
                    if i_id or i_qty
                ],
            })
    if not option_rows:
        option_rows = [
            {"key": "opt_0", "id": "", "name": "", "price": "0.00", "is_active": True, "ingredients": []},
        ]

    context = {
        "restaurant": restaurant,
        "available_menu_items": available_menu_items,
        "available_ingredients": available_ingredients,
        "error_message": error_message,
        "page_title": "Create Addon Group",
        "button_text": "Create Group",
        "form_name": request.POST.get("name", ""),
        "form_selection_type": request.POST.get("selection_type", AddonGroup.SelectionType.SINGLE),
        "form_is_required": request.POST.get("is_required") == "on" if request.method == "POST" else False,
        "form_min_selection": request.POST.get("min_selection", "0"),
        "form_max_selection": request.POST.get("max_selection", ""),
        "form_is_active": request.POST.get("is_active") == "on" if request.method == "POST" else True,
        "form_display_order": request.POST.get("display_order", "0"),
        "selected_menu_item_ids": [int(i) for i in request.POST.getlist("menu_items") if i.isdigit()],
        "option_rows": option_rows,
        "selection_type_choices": AddonGroup.SelectionType.choices,
    }
    return render(request, "menu/addon_group_form.html", context)


@manager_required
def addon_group_edit(request, group_id):
    restaurant = _get_active_restaurant(request)
    group = get_object_or_404(AddonGroup, id=group_id, restaurant=restaurant)

    available_menu_items = (
        MenuItem.objects.filter(category__restaurant=restaurant)
        .select_related("category")
        .order_by("category__name", "name")
    )
    available_ingredients = (
        Ingredient.objects.filter(restaurant=restaurant, is_active=True)
        .select_related("category")
        .order_by("category__name", "name")
    )

    error_message = None

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        selection_type = request.POST.get("selection_type", AddonGroup.SelectionType.SINGLE).strip().upper()
        is_required = request.POST.get("is_required") in ("on", "1", "true", "True")
        min_selection_text = request.POST.get("min_selection", "0").strip()
        max_selection_text = request.POST.get("max_selection", "").strip()
        is_active = request.POST.get("is_active") in ("on", "1", "true", "True")
        display_order_text = request.POST.get("display_order", "0").strip()
        menu_item_ids = request.POST.getlist("menu_items")

        try:
            min_selection = max(0, int(min_selection_text or 0))
        except ValueError:
            min_selection = 0

        max_selection = None
        if max_selection_text:
            try:
                max_selection = max(1, int(max_selection_text))
            except ValueError:
                max_selection = None

        try:
            display_order = max(0, int(display_order_text or 0))
        except ValueError:
            display_order = 0

        option_keys = request.POST.getlist("option_key[]")
        option_ids = request.POST.getlist("option_id[]")
        option_names = request.POST.getlist("option_name[]")
        option_prices = request.POST.getlist("option_price[]")
        option_actives = request.POST.getlist("option_active[]")

        options_data = []
        try:
            for idx, (opt_name, opt_price, opt_act) in enumerate(zip(option_names, option_prices, option_actives)):
                opt_name = opt_name.strip()
                if not opt_name:
                    continue
                try:
                    p = Decimal(opt_price.strip() or "0.00")
                    if p < 0:
                        p = Decimal("0.00")
                except (InvalidOperation, ValueError):
                    p = Decimal("0.00")
                is_act = opt_act in ("1", "true", "True", "on")

                opt_id_val = option_ids[idx] if idx < len(option_ids) else None
                key = option_keys[idx] if idx < len(option_keys) else (opt_id_val or f"opt_{idx}")
                ing_ids = request.POST.getlist(f"option_ingredient_{key}[]")
                ing_qtys = request.POST.getlist(f"option_quantity_{key}[]")

                ingredients_map = {}
                for ing_id, ing_qty in zip(ing_ids, ing_qtys):
                    ing_id = ing_id.strip()
                    ing_qty = ing_qty.strip()
                    if not ing_id and not ing_qty:
                        continue
                    if not ing_id:
                        raise ValidationError(f"Please select an ingredient for option '{opt_name}'.")
                    try:
                        qty = Decimal(ing_qty)
                        if qty <= 0 or not qty.is_finite():
                            raise ValidationError(f"Ingredient quantity must be greater than zero for option '{opt_name}'.")
                        ing_obj = available_ingredients.get(id=int(ing_id))
                        ingredients_map[ing_obj.id] = ingredients_map.get(ing_obj.id, Decimal("0")) + qty
                    except (InvalidOperation, ValueError, TypeError, Ingredient.DoesNotExist):
                        raise ValidationError(f"Select a valid active ingredient for option '{opt_name}'.")

                options_data.append({
                    "id": int(opt_id_val) if (opt_id_val and opt_id_val.isdigit()) else None,
                    "key": key,
                    "name": opt_name,
                    "price": p,
                    "is_active": is_act,
                    "display_order": idx,
                    "ingredients": ingredients_map,
                })
        except ValidationError as val_err:
            error_message = " ".join(val_err.messages) if hasattr(val_err, "messages") else str(val_err)

        if error_message is None:
            if not name:
                error_message = "Addon group name is required."
            elif not options_data:
                error_message = "Please add at least one option to this addon group."
            else:
                try:
                    with transaction.atomic():
                        group.name = name
                        group.selection_type = selection_type
                        group.is_required = is_required
                        group.min_selection = min_selection
                        group.max_selection = max_selection
                        group.is_active = is_active
                        group.display_order = display_order
                        group.full_clean()
                        group.save()

                        valid_items = MenuItem.objects.filter(
                            id__in=menu_item_ids,
                            category__restaurant=restaurant,
                        )
                        group.menu_items.set(valid_items)

                        existing_option_ids = set(group.options.values_list("id", flat=True))
                        kept_option_ids = set()
                        for opt in options_data:
                            if opt["id"] and opt["id"] in existing_option_ids:
                                group.options.filter(id=opt["id"]).update(
                                    name=opt["name"],
                                    price=opt["price"],
                                    is_active=opt["is_active"],
                                    display_order=opt["display_order"],
                                )
                                addon_opt = group.options.get(id=opt["id"])
                                kept_option_ids.add(opt["id"])
                            else:
                                addon_opt = AddonOption.objects.create(
                                    group=group,
                                    name=opt["name"],
                                    price=opt["price"],
                                    is_active=opt["is_active"],
                                    display_order=opt["display_order"],
                                )
                                kept_option_ids.add(addon_opt.id)

                            AddonOptionIngredient.objects.filter(addon_option=addon_opt).delete()
                            for ing_id, qty in opt["ingredients"].items():
                                AddonOptionIngredient.objects.create(
                                    addon_option=addon_opt,
                                    ingredient_id=ing_id,
                                    quantity=qty,
                                )

                        group.options.exclude(id__in=kept_option_ids).delete()

                    messages.success(request, f"Updated addon group '{group.name}' successfully.")
                    return redirect("addon_group_list")
                except ValidationError as e:
                    error_message = " ".join(e.messages) if hasattr(e, "messages") else str(e)

    option_rows = []
    if request.method == "POST":
        option_keys = request.POST.getlist("option_key[]")
        option_ids = request.POST.getlist("option_id[]")
        option_names = request.POST.getlist("option_name[]")
        option_prices = request.POST.getlist("option_price[]")
        option_actives = request.POST.getlist("option_active[]")
        for idx, (opt_name, opt_price, opt_act) in enumerate(zip(option_names, option_prices, option_actives)):
            opt_id_val = option_ids[idx] if idx < len(option_ids) else ""
            key = option_keys[idx] if idx < len(option_keys) else (opt_id_val or f"opt_{idx}")
            ing_ids = request.POST.getlist(f"option_ingredient_{key}[]")
            ing_qtys = request.POST.getlist(f"option_quantity_{key}[]")
            option_rows.append({
                "key": key,
                "id": opt_id_val,
                "name": opt_name,
                "price": opt_price,
                "is_active": opt_act in ("1", "true", "True", "on"),
                "ingredients": [
                    {"ingredient_id": int(i_id) if i_id.isdigit() else "", "quantity": i_qty}
                    for i_id, i_qty in zip(ing_ids, ing_qtys)
                    if i_id or i_qty
                ],
            })
    else:
        existing_options = group.options.prefetch_related(
            "ingredient_requirements__ingredient"
        ).order_by("display_order", "id")
        option_rows = [
            {
                "key": f"opt_{opt.id}",
                "id": opt.id,
                "name": opt.name,
                "price": str(opt.price),
                "is_active": opt.is_active,
                "ingredients": [
                    {
                        "ingredient_id": req.ingredient_id,
                        "quantity": str(req.quantity),
                    }
                    for req in opt.ingredient_requirements.all()
                ],
            }
            for opt in existing_options
        ]

    context = {
        "group": group,
        "restaurant": restaurant,
        "available_menu_items": available_menu_items,
        "available_ingredients": available_ingredients,
        "error_message": error_message,
        "page_title": f"Edit {group.name}",
        "button_text": "Save Changes",
        "form_name": request.POST.get("name", group.name),
        "form_selection_type": request.POST.get("selection_type", group.selection_type),
        "form_is_required": request.POST.get("is_required") == "on" if request.method == "POST" else group.is_required,
        "form_min_selection": request.POST.get("min_selection", str(group.min_selection)),
        "form_max_selection": request.POST.get("max_selection", str(group.max_selection or "")),
        "form_is_active": request.POST.get("is_active") == "on" if request.method == "POST" else group.is_active,
        "form_display_order": request.POST.get("display_order", str(group.display_order)),
        "selected_menu_item_ids": (
            [int(i) for i in request.POST.getlist("menu_items") if i.isdigit()]
            if request.method == "POST"
            else list(group.menu_items.values_list("id", flat=True))
        ),
        "option_rows": option_rows or [{"key": "opt_0", "id": "", "name": "", "price": "0.00", "is_active": True, "ingredients": []}],
        "selection_type_choices": AddonGroup.SelectionType.choices,
    }
    return render(request, "menu/addon_group_form.html", context)


@manager_required
@require_POST
def addon_group_delete(request, group_id):
    restaurant = _get_active_restaurant(request)
    group = get_object_or_404(AddonGroup, id=group_id, restaurant=restaurant)
    name = group.name
    group.delete()
    messages.success(request, f"Addon group '{name}' deleted.")
    return redirect("addon_group_list")

