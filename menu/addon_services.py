from decimal import Decimal
from django.core.exceptions import ValidationError

from menu.models import AddonGroup, AddonOption, MenuItem


def normalize_addon_ids(raw_ids):
    """
    Safely convert an iterable or comma-separated string of IDs into a list of integers.
    """
    if not raw_ids:
        return []
    if isinstance(raw_ids, str):
        raw_ids = [part.strip() for part in raw_ids.split(",") if part.strip()]
    normalized = []
    for item in raw_ids:
        try:
            val = int(item)
            if val > 0:
                normalized.append(val)
        except (ValueError, TypeError):
            continue
    return normalized


def validate_item_addons(menu_item, selected_addon_ids):
    """
    Validate selected addon option IDs for a MenuItem.

    Enforces:
    - Active addon groups assigned to the menu item.
    - Active addon options belonging to those groups and the same restaurant.
    - Required selection rules.
    - Single selection rules (max 1).
    - Multiple selection min/max constraints.
    - No duplicate selections of the same option.

    Returns:
        tuple (validated_addon_options: list[AddonOption], unit_addons_total: Decimal)
    Raises:
        ValidationError if any rule is violated.
    """
    selected_ids = normalize_addon_ids(selected_addon_ids)

    # Active groups assigned to this item for this item's restaurant
    assigned_groups = (
        menu_item.addon_groups.filter(
            is_active=True,
            restaurant=menu_item.category.restaurant,
        )
        .prefetch_related("options__ingredient_requirements__ingredient")
        .order_by("display_order", "id")
    )

    allowed_options_map = {}
    for group in assigned_groups:
        for opt in group.options.all():
            if opt.is_active:
                opt.group = group
                allowed_options_map[opt.id] = opt

    # Check for invalid or unavailable addon IDs
    from inventory.services import calculate_addon_available_portions

    validated_options = []
    seen_option_ids = set()
    for opt_id in selected_ids:
        if opt_id in seen_option_ids:
            raise ValidationError("Duplicate addon option selected.")
        seen_option_ids.add(opt_id)

        opt = allowed_options_map.get(opt_id)
        if not opt:
            raise ValidationError(
                "One or more selected addons are invalid or unavailable for this item."
            )
        if calculate_addon_available_portions(opt) <= 0:
            raise ValidationError(
                f"Addon '{opt.name}' is currently out of stock."
            )
        validated_options.append(opt)

    # Validate rules per group
    for group in assigned_groups:
        group_selected = [opt for opt in validated_options if opt.group_id == group.id]
        selected_count = len(group_selected)

        if group.selection_type == AddonGroup.SelectionType.SINGLE:
            if selected_count > 1:
                raise ValidationError(
                    f"You can select at most one option for '{group.name}'."
                )
            if group.is_required and selected_count == 0:
                raise ValidationError(
                    f"Please select an option for '{group.name}'."
                )
        else:
            # MULTIPLE selection
            min_req = group.min_selection
            if group.is_required and min_req < 1:
                min_req = 1

            if selected_count < min_req:
                if min_req == 1:
                    raise ValidationError(
                        f"Please select at least 1 option for '{group.name}'."
                    )
                raise ValidationError(
                    f"Please select at least {min_req} options for '{group.name}'."
                )

            if group.max_selection is not None and selected_count > group.max_selection:
                raise ValidationError(
                    f"You can select at most {group.max_selection} option(s) for '{group.name}'."
                )

    unit_addons_total = sum(
        (opt.price for opt in validated_options),
        Decimal("0.00"),
    )
    return validated_options, unit_addons_total
