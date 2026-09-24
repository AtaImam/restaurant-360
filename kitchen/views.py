from datetime import datetime, timedelta
from decimal import Decimal
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Avg, Count, F, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from business_settings.services import resolve_settings
from inventory.models import (
    BranchIngredientStock,
    Ingredient,
    IngredientCategory,
    IngredientRequest,
    Recipe,
    RecipeIngredient,
)
from menu.models import BranchMenuItemOverride, Category, MenuItem
from orders.models import Order, OrderItem
from orders.services import get_live_order_cutoff, orders_for_user, transition_order_status
from restaurant.branch_services import get_active_branch
from restaurant.models import Restaurant
from staff.models import (
    Attendance,
    EmployeeProfile,
    LeaveRequest,
    OrderStaffService,
    PayrollRecord,
    SalaryAdvance,
    Shift,
    StaffNotification,
)
from staff.operations import local_work_date
from staff.services import check_in_employee, check_out_employee


def _get_kitchen_context(request):
    """Ensure user has kitchen or management permissions and return common context."""
    user = request.user
    if not user.is_authenticated:
        raise PermissionDenied("Authentication required.")

    if not (getattr(user, "can_access_kitchen", None) and user.can_access_kitchen()):
        raise PermissionDenied("Customers and unauthorized roles cannot access the kitchen interface.")

    restaurant = getattr(user, "restaurant", None) or Restaurant.objects.first()
    active_branch = get_active_branch(request, restaurant) if restaurant else None

    profile = getattr(user, "employee_profile", None)
    if profile is None:
        profile = EmployeeProfile.objects.filter(user=user).first()

    return user, profile, active_branch, restaurant


def kitchen_operator_required(view_func):
    """Restricts access to interactive kitchen operators (chief, kitchen_manager).
    Redirects owner, manager, and admin to the single read-only kitchen_dashboard for GET requests.
    Raises PermissionDenied for modifying actions.
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated:
            return redirect("/auth/login/")
        if not (getattr(user, "can_operate_kitchen", None) and user.can_operate_kitchen()):
            if request.method == "POST":
                raise PermissionDenied("Only Chief or Kitchen Manager can perform kitchen operations.")
            return redirect("kitchen_dashboard")
        return view_func(request, *args, **kwargs)
    return _wrapped_view


def calculate_portions_possible(menu_item, active_branch):
    """Calculate maximum portions possible for a menu item based on current ingredient stocks."""
    if not hasattr(menu_item, "recipe"):
        return None

    recipe = menu_item.recipe
    recipe_ingredients = list(recipe.recipe_ingredients.select_related("ingredient").all())
    if not recipe_ingredients:
        return None

    yield_qty = recipe.yield_quantity if recipe.yield_quantity > 0 else Decimal("1")
    portions_list = []

    for ri in recipe_ingredients:
        if ri.quantity <= 0:
            continue

        per_portion = ri.quantity / yield_qty
        if per_portion <= 0:
            continue

        avail = Decimal("0.000")
        if active_branch:
            branch_stock = BranchIngredientStock.objects.filter(
                branch=active_branch,
                ingredient=ri.ingredient,
            ).first()
            if branch_stock:
                avail = branch_stock.available_stock
        else:
            # Aggregate across branches if no branch selected
            stocks = BranchIngredientStock.objects.filter(ingredient=ri.ingredient)
            avail = sum((s.available_stock for s in stocks), Decimal("0.000"))

        portions_possible = int(avail // per_portion)
        portions_list.append(max(0, portions_possible))

    return min(portions_list) if portions_list else None


# ===========================================================================
# 1. KITCHEN DISPLAY / KDS
# ===========================================================================

@login_required(login_url="/auth/login/")
def kitchen_dashboard(request):
    user, profile, active_branch, restaurant = _get_kitchen_context(request)
    is_kitchen_operator = getattr(user, "role", None) in ["chief", "kitchen_manager"]
    is_read_only = not is_kitchen_operator
    operating_settings = resolve_settings(restaurant, active_branch)
    live_cutoff = get_live_order_cutoff()

    orders_qs = (
        orders_for_user(user)
        .filter(status__in=["NEW", "ACCEPTED", "PREPARING", "READY"])
        .filter(created_at__gte=live_cutoff)
    )

    if active_branch and (user.role in ("owner", "admin", "manager") or not getattr(user, "branch_id", None)):
        orders_qs = orders_qs.filter(branch=active_branch)

    orders = (
        orders_qs
        .select_related("table", "table__floor", "staff_service__waiter__user")
        .prefetch_related("items__menu_item", "items__addons")
        .order_by("created_at" if operating_settings.get("kitchen_sort") == "oldest" else "-created_at")
    )

    if not operating_settings.get("kitchen_show_dine_in", True):
        orders = orders.exclude(order_type="DINE_IN")
    if not operating_settings.get("kitchen_show_takeaway", True):
        orders = orders.exclude(order_type="TAKEAWAY")

    now = timezone.now()
    warning_minutes = operating_settings.get("kitchen_warning_minutes") or 15

    kpi_new = 0
    kpi_accepted = 0
    kpi_preparing = 0
    kpi_ready = 0
    kpi_delayed = 0

    for order in orders:
        if order.status == "NEW":
            kpi_new += 1
        elif order.status == "ACCEPTED":
            kpi_accepted += 1
        elif order.status == "PREPARING":
            kpi_preparing += 1
        elif order.status == "READY":
            kpi_ready += 1

        since = order.status_changed_at or order.created_at
        elapsed_seconds = int((now - since).total_seconds())
        order.elapsed_seconds = max(0, elapsed_seconds)
        order.elapsed_minutes = order.elapsed_seconds // 60
        order.kitchen_delayed = order.elapsed_minutes >= warning_minutes
        if order.kitchen_delayed:
            kpi_delayed += 1

        # Dining & Source Attribution
        if order.order_type == "DINE_IN" and order.table:
            floor_name = f" ({order.table.floor.name})" if getattr(order.table, "floor", None) else ""
            table_num = getattr(order.table, "table_number", getattr(order.table, "number", order.table.id))
            order.location_display = f"Table {table_num}{floor_name}"
        else:
            order.location_display = "Takeaway"

        staff_svc = getattr(order, "staff_service", None)
        if staff_svc and staff_svc.order_taken_by:
            order.source_display = "Staff POS"
        else:
            order.source_display = "Customer QR"

        order.waiter_name = staff_svc.waiter.user.get_full_name() if (staff_svc and staff_svc.waiter) else "Unassigned"

        # Next action state (forward-only: NEW -> ACCEPTED -> PREPARING -> READY)
        if is_kitchen_operator:
            if order.status == "NEW":
                order.next_status = "ACCEPTED"
                order.next_label = "Accept Order"
                order.next_class = "btn-accept"
            elif order.status == "ACCEPTED":
                order.next_status = "PREPARING"
                order.next_label = "Start Preparing"
                order.next_class = "btn-prepare"
            elif order.status == "PREPARING":
                order.next_status = "READY"
                order.next_label = "Mark Ready"
                order.next_class = "btn-ready"
            else:
                order.next_status = None
                order.next_label = None
                order.next_class = None
        else:
            order.next_status = None
            order.next_label = None
            order.next_class = None

    if is_kitchen_operator:
        # Real Average Prep Duration (creation to READY / COMPLETED today)
        today = local_work_date()
        base_orders = Order.objects.filter(restaurant=restaurant)
        if active_branch:
            base_orders = base_orders.filter(branch=active_branch)

        today_ready_orders = base_orders.filter(
            created_at__date=today,
            status__in=["READY", "SERVED", "COMPLETED"],
            status_changed_at__isnull=False,
        )
        prep_durations = []
        for o in today_ready_orders:
            diff_mins = (o.status_changed_at - o.created_at).total_seconds() / 60.0
            if 0 < diff_mins < 300:
                prep_durations.append(diff_mins)
        avg_prep_time = round(sum(prep_durations) / len(prep_durations), 1) if prep_durations else None

        # Real Average Waiter Pickup Duration (READY to SERVED today)
        service_qs = OrderStaffService.objects.filter(
            order__restaurant=restaurant,
            ready_at__isnull=False,
            served_at__isnull=False,
        )
        if active_branch:
            service_qs = service_qs.filter(order__branch=active_branch)

        pickup_durations = []
        for s in service_qs.filter(served_at__date=today):
            if s.served_at > s.ready_at:
                p_diff = (s.served_at - s.ready_at).total_seconds() / 60.0
                if 0 < p_diff < 180:
                    pickup_durations.append(p_diff)
        avg_pickup_time = round(sum(pickup_durations) / len(pickup_durations), 1) if pickup_durations else None

        # 7-Day Trend of Kitchen Activity (Orders Received vs Prepared)
        trend_data = []
        for i in range(6, -1, -1):
            day_date = today - timedelta(days=i)
            day_label = day_date.strftime("%b %d")
            short_day = day_date.strftime("%a")
            day_received = base_orders.filter(created_at__date=day_date).count()
            day_prepared = base_orders.filter(
                created_at__date=day_date,
                status__in=["READY", "SERVED", "COMPLETED"],
            ).count()
            trend_data.append({
                "day": day_label,
                "short_day": short_day,
                "date": day_date.isoformat(),
                "received": day_received,
                "prepared": day_prepared,
            })
        max_trend = max([max(d["received"], d["prepared"]) for d in trend_data] + [1])

        # Low-Stock Summary & Stock Percentages
        low_stock_qs = BranchIngredientStock.objects.filter(
            branch=active_branch
        ) if active_branch else BranchIngredientStock.objects.filter(
            ingredient__restaurant=restaurant
        )
        low_stock_list = []
        for s in low_stock_qs.select_related("ingredient", "ingredient__category"):
            if s.available_stock <= s.min_stock_alert or s.current_stock <= 0:
                stock_pct = 0
                if s.min_stock_alert > 0:
                    stock_pct = min(100, int((s.available_stock / s.min_stock_alert) * 100))
                low_stock_list.append({
                    "ingredient": s.ingredient,
                    "available_stock": s.available_stock,
                    "min_alert": s.min_stock_alert,
                    "unit": s.ingredient.get_base_unit_display() if hasattr(s.ingredient, "get_base_unit_display") else s.ingredient.base_unit,
                    "stock_percent": stock_pct,
                    "is_out": s.available_stock <= 0,
                })
        low_stock_list.sort(key=lambda x: (not x["is_out"], x["available_stock"]))
        low_stock_summary = low_stock_list[:5]
        low_stock_count = len(low_stock_list)

        # Urgent Ingredient Requests Summary
        req_qs = IngredientRequest.objects.filter(restaurant=restaurant)
        if active_branch:
            req_qs = req_qs.filter(branch=active_branch)
        pending_reqs = req_qs.filter(status="PENDING").select_related("ingredient", "requested_by").order_by("-created_at")
        urgent_requests = [r for r in pending_reqs if r.priority in ["URGENT", "HIGH"]][:5]
        pending_requests_count = pending_reqs.count()
    else:
        avg_prep_time = None
        avg_pickup_time = None
        trend_data = []
        max_trend = 1
        low_stock_summary = []
        low_stock_count = 0
        urgent_requests = []
        pending_requests_count = 0

    context = {
        "orders": orders,
        "kpi_new": kpi_new,
        "kpi_accepted": kpi_accepted,
        "kpi_preparing": kpi_preparing,
        "kpi_ready": kpi_ready,
        "kpi_delayed": kpi_delayed,
        "kpi_total_active": len(orders),
        "avg_prep_time": avg_prep_time,
        "avg_pickup_time": avg_pickup_time,
        "trend_data": trend_data,
        "max_trend": max_trend,
        "low_stock_summary": low_stock_summary,
        "low_stock_count": low_stock_count,
        "urgent_requests": urgent_requests,
        "pending_requests_count": pending_requests_count,
        "operating_settings": operating_settings,
        "active_branch": active_branch,
        "restaurant": restaurant,
        "is_kitchen_operator": is_kitchen_operator,
        "is_read_only": is_read_only,
        "active_tab": "kds",
        "unread_notifications_count": StaffNotification.objects.filter(recipient=user, is_read=False).count(),
        "notifications": StaffNotification.objects.filter(recipient=user).order_by("-created_at")[:10],
    }
    return render(request, "kitchen/dashboard.html", context)


@login_required(login_url="/auth/login/")
@kitchen_operator_required
def kitchen_order_queue(request):
    user, profile, active_branch, restaurant = _get_kitchen_context(request)
    is_kitchen_operator = getattr(user, "role", None) in ["chief", "kitchen_manager"]
    is_read_only = not is_kitchen_operator
    operating_settings = resolve_settings(restaurant, active_branch)
    live_cutoff = get_live_order_cutoff()

    status_filter = request.GET.get("status", "ALL").upper()
    order_type = request.GET.get("type", "ALL").upper()
    search = request.GET.get("search", "").strip()

    orders_qs = (
        orders_for_user(user)
        .filter(status__in=["NEW", "ACCEPTED", "PREPARING", "READY"])
        .filter(created_at__gte=live_cutoff)
    )
    if active_branch and (user.role in ("owner", "admin", "manager") or not getattr(user, "branch_id", None)):
        orders_qs = orders_qs.filter(branch=active_branch)

    if order_type in ["DINE_IN", "TAKEAWAY"]:
        orders_qs = orders_qs.filter(order_type=order_type)

    if search:
        if search.isdigit():
            orders_qs = orders_qs.filter(Q(id=int(search)) | Q(table__table_number__icontains=search))
        else:
            orders_qs = orders_qs.filter(
                Q(table__table_number__icontains=search)
                | Q(notes__icontains=search)
                | Q(items__menu_item__name__icontains=search)
            ).distinct()

    orders = (
        orders_qs
        .select_related("table", "table__floor", "staff_service__waiter__user")
        .prefetch_related("items__menu_item", "items__addons")
        .order_by("created_at" if operating_settings.get("kitchen_sort") == "oldest" else "-created_at")
    )

    now = timezone.now()
    warning_minutes = operating_settings.get("kitchen_warning_minutes") or 15

    orders_list = []
    kpi_new = 0
    kpi_accepted = 0
    kpi_preparing = 0
    kpi_ready = 0
    kpi_delayed = 0

    for order in orders:
        if order.status == "NEW":
            kpi_new += 1
        elif order.status == "ACCEPTED":
            kpi_accepted += 1
        elif order.status == "PREPARING":
            kpi_preparing += 1
        elif order.status == "READY":
            kpi_ready += 1

        since = order.status_changed_at or order.created_at
        elapsed_seconds = int((now - since).total_seconds())
        order.elapsed_seconds = max(0, elapsed_seconds)
        order.elapsed_minutes = order.elapsed_seconds // 60
        order.kitchen_delayed = order.elapsed_minutes >= warning_minutes
        if order.kitchen_delayed:
            kpi_delayed += 1

        if order.order_type == "DINE_IN" and order.table:
            floor_name = f" ({order.table.floor.name})" if getattr(order.table, "floor", None) else ""
            table_num = getattr(order.table, "table_number", getattr(order.table, "number", order.table.id))
            order.location_display = f"Table {table_num}{floor_name}"
        else:
            order.location_display = "Takeaway"

        staff_svc = getattr(order, "staff_service", None)
        if staff_svc and staff_svc.order_taken_by:
            order.source_display = "Staff POS"
        else:
            order.source_display = "Customer QR"

        order.waiter_name = staff_svc.waiter.user.get_full_name() if (staff_svc and staff_svc.waiter) else "Unassigned"

        if is_kitchen_operator:
            if order.status == "NEW":
                order.next_status = "ACCEPTED"
                order.next_label = "Accept"
                order.next_class = "btn-accept"
            elif order.status == "ACCEPTED":
                order.next_status = "PREPARING"
                order.next_label = "Start Prep"
                order.next_class = "btn-prepare"
            elif order.status == "PREPARING":
                order.next_status = "READY"
                order.next_label = "Mark Ready"
                order.next_class = "btn-ready"
            else:
                order.next_status = None
                order.next_label = None
                order.next_class = None
        else:
            order.next_status = None
            order.next_label = None
            order.next_class = None

        if status_filter == "DELAYED":
            if order.kitchen_delayed:
                orders_list.append(order)
        elif status_filter in ["NEW", "ACCEPTED", "PREPARING", "READY"]:
            if order.status == status_filter:
                orders_list.append(order)
        else:
            orders_list.append(order)

    context = {
        "orders": orders_list,
        "status_filter": status_filter,
        "order_type": order_type,
        "search": search,
        "kpi_new": kpi_new,
        "kpi_accepted": kpi_accepted,
        "kpi_preparing": kpi_preparing,
        "kpi_ready": kpi_ready,
        "kpi_delayed": kpi_delayed,
        "kpi_total": len(orders),
        "operating_settings": operating_settings,
        "active_branch": active_branch,
        "restaurant": restaurant,
        "is_kitchen_operator": is_kitchen_operator,
        "is_read_only": is_read_only,
        "active_tab": "orders",
        "unread_notifications_count": StaffNotification.objects.filter(recipient=user, is_read=False).count(),
        "notifications": StaffNotification.objects.filter(recipient=user).order_by("-created_at")[:10],
    }
    return render(request, "kitchen/orders.html", context)


@require_POST
@login_required(login_url="/auth/login/")
@kitchen_operator_required
def update_order_status(request, order_id):
    user, _, active_branch, restaurant = _get_kitchen_context(request)
    if getattr(user, "role", None) not in ["chief", "kitchen_manager"]:
        raise PermissionDenied("Only kitchen staff (Chief or Kitchen Manager) can update kitchen order status.")

    order_qs = orders_for_user(user)
    if active_branch and (user.role in ("owner", "admin", "manager") or not getattr(user, "branch_id", None)):
        order_qs = order_qs.filter(branch=active_branch)

    order = get_object_or_404(order_qs, id=order_id)
    target_status = request.POST.get("status")

    try:
        transition_order_status(
            order,
            target_status,
            allowed_targets={"ACCEPTED", "PREPARING", "READY"},
        )
        if target_status == "READY":
            messages.success(request, f"Order #{order.id} is marked READY. Waiter has been notified!")
        else:
            messages.success(request, f"Order #{order.id} transitioned to {target_status}.")
    except ValidationError as error:
        messages.error(request, " ".join(error.messages if hasattr(error, "messages") else [str(error)]))

    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url:
        return redirect(next_url)
    return redirect("kitchen_dashboard")


# ===========================================================================
# 2. ORDER HISTORY
# ===========================================================================

@login_required(login_url="/auth/login/")
@kitchen_operator_required
@require_GET
def kitchen_order_history(request):
    user, _, active_branch, restaurant = _get_kitchen_context(request)
    period = request.GET.get("period", "today")
    search = request.GET.get("search", "").strip()

    orders_qs = orders_for_user(user)
    if active_branch and not getattr(user, "branch_id", None):
        orders_qs = orders_qs.filter(branch=active_branch)

    now = timezone.now()
    if period == "today":
        today = local_work_date()
        orders_qs = orders_qs.filter(created_at__date=today)
    elif period == "yesterday":
        yesterday = local_work_date() - timedelta(days=1)
        orders_qs = orders_qs.filter(created_at__date=yesterday)
    elif period == "week":
        orders_qs = orders_qs.filter(created_at__gte=now - timedelta(days=7))

    if search:
        if search.isdigit():
            orders_qs = orders_qs.filter(id=int(search))
        else:
            orders_qs = orders_qs.filter(table__table_number__icontains=search)

    orders = (
        orders_qs
        .select_related("table", "table__floor", "staff_service__waiter__user")
        .prefetch_related("items__menu_item")
        .order_by("-created_at")[:100]
    )

    orders_list = []
    for o in orders:
        prep_time_minutes = None
        if o.status_changed_at and o.created_at:
            prep_time_minutes = int((o.status_changed_at - o.created_at).total_seconds() // 60)
        o.prep_time_minutes = prep_time_minutes
        orders_list.append(o)

    context = {
        "orders": orders_list,
        "period": period,
        "search": search,
        "active_branch": active_branch,
        "restaurant": restaurant,
        "active_tab": "order_history",
        "unread_notifications_count": StaffNotification.objects.filter(recipient=user, is_read=False).count(),
        "notifications": StaffNotification.objects.filter(recipient=user).order_by("-created_at")[:10],
    }
    return render(request, "kitchen/order_history.html", context)


# ===========================================================================
# 3. MENU ITEMS & ITEM AVAILABILITY / SOLD OUT
# ===========================================================================

@login_required(login_url="/auth/login/")
@kitchen_operator_required
def kitchen_menu_items(request):
    user, _, active_branch, restaurant = _get_kitchen_context(request)
    category_id = request.GET.get("category")
    avail_filter = request.GET.get("availability", "ALL").upper()
    search = request.GET.get("search", "").strip()

    items_qs = MenuItem.objects.filter(category__restaurant=restaurant).select_related("category")
    if category_id and category_id.isdigit():
        items_qs = items_qs.filter(category_id=int(category_id))
    if search:
        items_qs = items_qs.filter(name__icontains=search)

    categories = Category.objects.filter(restaurant=restaurant).order_by("name")

    # Prefetch overrides if active branch
    overrides_map = {}
    if active_branch:
        overrides = BranchMenuItemOverride.objects.filter(branch=active_branch)
        for ov in overrides:
            overrides_map[ov.menu_item_id] = ov

    items_data = []
    for item in items_qs.order_by("category__name", "name"):
        ov = overrides_map.get(item.id)
        if ov is not None:
            effective_avail = ov.is_available and item.is_available
        else:
            effective_avail = item.is_available

        if avail_filter == "AVAILABLE" and not effective_avail:
            continue
        elif avail_filter == "SOLDOUT" and effective_avail:
            continue

        portions = calculate_portions_possible(item, active_branch)
        items_data.append({
            "item": item,
            "effective_available": effective_avail,
            "portions_possible": portions,
            "has_recipe": hasattr(item, "recipe"),
        })

    context = {
        "items_data": items_data,
        "categories": categories,
        "selected_category": int(category_id) if category_id and category_id.isdigit() else None,
        "avail_filter": avail_filter,
        "search": search,
        "active_branch": active_branch,
        "restaurant": restaurant,
        "active_tab": "menu_items",
        "unread_notifications_count": StaffNotification.objects.filter(recipient=user, is_read=False).count(),
        "notifications": StaffNotification.objects.filter(recipient=user).order_by("-created_at")[:10],
    }
    return render(request, "kitchen/menu_items.html", context)


@require_POST
@login_required(login_url="/auth/login/")
@kitchen_operator_required
def toggle_item_availability(request, item_id):
    user, _, active_branch, restaurant = _get_kitchen_context(request)
    item = get_object_or_404(MenuItem, id=item_id, category__restaurant=restaurant)

    if active_branch:
        override, created = BranchMenuItemOverride.objects.get_or_create(
            branch=active_branch,
            menu_item=item,
            defaults={"is_available": item.is_available},
        )
        override.is_available = not override.is_available
        override.save(update_fields=["is_available"])
        status_text = "Available" if override.is_available else "Sold Out"
        messages.success(request, f'"{item.name}" is now marked as {status_text} in {active_branch.name}.')
    else:
        item.is_available = not item.is_available
        item.save(update_fields=["is_available"])
        status_text = "Available" if item.is_available else "Sold Out"
        messages.success(request, f'"{item.name}" is now marked as {status_text}.')

    return redirect(request.POST.get("next") or "kitchen_menu_items")


# ===========================================================================
# 4. RECIPES
# ===========================================================================

@login_required(login_url="/auth/login/")
@kitchen_operator_required
def kitchen_recipes(request):
    user, _, active_branch, restaurant = _get_kitchen_context(request)
    search = request.GET.get("search", "").strip()
    category_id = request.GET.get("category")

    recipes_qs = Recipe.objects.filter(
        menu_item__category__restaurant=restaurant
    ).select_related("menu_item", "menu_item__category").prefetch_related(
        "recipe_ingredients__ingredient"
    ).order_by("menu_item__name")

    if search:
        recipes_qs = recipes_qs.filter(menu_item__name__icontains=search)
    if category_id and category_id.isdigit():
        recipes_qs = recipes_qs.filter(menu_item__category_id=int(category_id))

    categories = Category.objects.filter(restaurant=restaurant).order_by("name")

    # Stocks lookup map for current branch
    stock_map = {}
    if active_branch:
        stocks = BranchIngredientStock.objects.filter(branch=active_branch)
        for s in stocks:
            stock_map[s.ingredient_id] = s.available_stock
    else:
        stocks = BranchIngredientStock.objects.filter(ingredient__restaurant=restaurant)
        for s in stocks:
            stock_map[s.ingredient_id] = stock_map.get(s.ingredient_id, Decimal("0.000")) + s.available_stock

    recipes_data = []
    for r in recipes_qs:
        ing_data = []
        shortages = []
        for ri in r.recipe_ingredients.all():
            avail = stock_map.get(ri.ingredient_id, Decimal("0.000"))
            unit_str = ri.ingredient.get_base_unit_display() if hasattr(ri.ingredient, "get_base_unit_display") else ri.ingredient.base_unit
            shortage_amount = max(Decimal("0.000"), ri.quantity - avail)
            has_shortage = shortage_amount > Decimal("0.000")
            if has_shortage:
                shortages.append({
                    "ingredient": ri.ingredient,
                    "required": ri.quantity,
                    "available": avail,
                    "shortage": shortage_amount,
                    "unit": unit_str,
                })
            ing_data.append({
                "ingredient": ri.ingredient,
                "quantity": ri.quantity,
                "unit": unit_str,
                "available_stock": avail,
                "has_shortage": has_shortage,
                "shortage": shortage_amount,
            })
        recipes_data.append({
            "recipe": r,
            "ingredients": ing_data,
            "shortages": shortages,
            "has_shortages": len(shortages) > 0,
            "portions_possible": calculate_portions_possible(r.menu_item, active_branch),
        })

    context = {
        "recipes_data": recipes_data,
        "categories": categories,
        "selected_category": int(category_id) if category_id and category_id.isdigit() else None,
        "search": search,
        "active_branch": active_branch,
        "restaurant": restaurant,
        "active_tab": "recipes",
        "unread_notifications_count": StaffNotification.objects.filter(recipient=user, is_read=False).count(),
        "notifications": StaffNotification.objects.filter(recipient=user).order_by("-created_at")[:10],
    }
    return render(request, "kitchen/recipes.html", context)


# ===========================================================================
# 5. INGREDIENTS & LOW STOCK VIEW
# ===========================================================================

@login_required(login_url="/auth/login/")
@kitchen_operator_required
def kitchen_ingredients(request):
    user, _, active_branch, restaurant = _get_kitchen_context(request)
    search = request.GET.get("search", "").strip()
    category_id = request.GET.get("category")
    status_filter = request.GET.get("status", "ALL").upper()

    ingredients_qs = Ingredient.objects.filter(restaurant=restaurant).select_related("category")
    if search:
        ingredients_qs = ingredients_qs.filter(Q(name__icontains=search) | Q(sku__icontains=search))
    if category_id and category_id.isdigit():
        ingredients_qs = ingredients_qs.filter(category_id=int(category_id))

    categories = IngredientCategory.objects.filter(restaurant=restaurant).order_by("name")

    # Branch stocks
    stock_map = {}
    if active_branch:
        stocks = BranchIngredientStock.objects.filter(branch=active_branch)
        for s in stocks:
            stock_map[s.ingredient_id] = s

    ingredients_data = []
    for ing in ingredients_qs.order_by("name"):
        bs = stock_map.get(ing.id)
        current_stock = bs.current_stock if bs else Decimal("0.000")
        available_stock = bs.available_stock if bs else Decimal("0.000")
        min_stock = bs.min_stock_alert if bs else Decimal("0.000")
        status = bs.stock_status if bs else "OUT"

        stock_percent = 0
        if min_stock > 0:
            stock_percent = min(100, int((available_stock / min_stock) * 100))
        elif available_stock > 0:
            stock_percent = 100

        if available_stock <= 0:
            status_level = "CRITICAL"
            status_label = "Out of Stock"
            badge_class = "badge-danger"
        elif available_stock <= min_stock:
            status_level = "LOW"
            status_label = "Low Stock"
            badge_class = "badge-warning"
        else:
            status_level = "HEALTHY"
            status_label = "In Stock"
            badge_class = "badge-success"

        item_data = {
            "ingredient": ing,
            "current_stock": current_stock,
            "available_stock": available_stock,
            "min_stock_alert": min_stock,
            "stock_status": status,
            "stock_percent": stock_percent,
            "status_level": status_level,
            "status_label": status_label,
            "badge_class": badge_class,
            "suggested_qty": max(Decimal("10.000"), (min_stock * Decimal("2")) - available_stock),
        }

        if status_filter == "CRITICAL" and status_level != "CRITICAL":
            continue
        elif status_filter == "LOW" and status_level not in ["CRITICAL", "LOW"]:
            continue
        elif status_filter == "HEALTHY" and status_level != "HEALTHY":
            continue

        ingredients_data.append(item_data)

    context = {
        "ingredients_data": ingredients_data,
        "categories": categories,
        "selected_category": int(category_id) if category_id and category_id.isdigit() else None,
        "status_filter": status_filter,
        "search": search,
        "active_branch": active_branch,
        "restaurant": restaurant,
        "active_tab": "ingredients",
        "unread_notifications_count": StaffNotification.objects.filter(recipient=user, is_read=False).count(),
        "notifications": StaffNotification.objects.filter(recipient=user).order_by("-created_at")[:10],
    }
    return render(request, "kitchen/ingredients.html", context)


@login_required(login_url="/auth/login/")
@kitchen_operator_required
def kitchen_low_stock(request):
    user, _, active_branch, restaurant = _get_kitchen_context(request)

    # Filter only low/out stocks in active_branch
    low_stocks = []
    stock_source = (
        BranchIngredientStock.objects.filter(branch=active_branch)
        if active_branch
        else BranchIngredientStock.objects.filter(ingredient__restaurant=restaurant)
    )

    stocks = (
        stock_source
        .select_related("ingredient", "ingredient__category")
        .filter(Q(current_stock__lte=F("min_stock_alert")) | Q(current_stock__lte=0))
        .order_by("current_stock")
    )
    for bs in stocks:
        stock_pct = 0
        if bs.min_stock_alert > 0:
            stock_pct = min(100, int((bs.available_stock / bs.min_stock_alert) * 100))
        low_stocks.append({
            "ingredient": bs.ingredient,
            "current_stock": bs.current_stock,
            "available_stock": bs.available_stock,
            "min_stock_alert": bs.min_stock_alert,
            "stock_status": bs.stock_status,
            "stock_percent": stock_pct,
            "suggested_qty": max(Decimal("10.000"), (bs.min_stock_alert * Decimal("2")) - bs.available_stock),
        })

    context = {
        "low_stocks": low_stocks,
        "active_branch": active_branch,
        "restaurant": restaurant,
        "active_tab": "low_stock",
        "unread_notifications_count": StaffNotification.objects.filter(recipient=user, is_read=False).count(),
        "notifications": StaffNotification.objects.filter(recipient=user).order_by("-created_at")[:10],
    }
    return render(request, "kitchen/low_stock.html", context)


# ===========================================================================
# 6. INGREDIENT REQUESTS (KITCHEN WORKFLOW)
# ===========================================================================

@login_required(login_url="/auth/login/")
@kitchen_operator_required
def kitchen_ingredient_requests(request):
    user, _, active_branch, restaurant = _get_kitchen_context(request)

    if request.method == "POST":
        ingredient_id = request.POST.get("ingredient_id")
        quantity_str = request.POST.get("quantity", "").strip()
        priority = request.POST.get("priority", "MEDIUM")
        needed_date_str = request.POST.get("needed_date", "").strip()
        reason = request.POST.get("reason", "").strip()

        try:
            ingredient = get_object_or_404(Ingredient, id=ingredient_id, restaurant=restaurant)
            quantity = Decimal(quantity_str)
            if quantity <= 0:
                raise ValidationError("Quantity must be greater than zero.")

            needed_date = None
            if needed_date_str:
                needed_date = datetime.strptime(needed_date_str, "%Y-%m-%d").date()

            IngredientRequest.objects.create(
                restaurant=restaurant,
                branch=active_branch,
                ingredient=ingredient,
                quantity=quantity,
                priority=priority,
                needed_date=needed_date,
                reason=reason,
                status=IngredientRequest.Status.PENDING,
                requested_by=user,
            )
            messages.success(request, f'Stock request for {quantity} {ingredient.name} submitted successfully.')
            return redirect("kitchen_ingredient_requests")
        except (ValueError, ValidationError) as err:
            messages.error(request, " ".join(err.messages if hasattr(err, "messages") else [str(err)]))

    status_filter = request.GET.get("status", "all")
    requests_qs = IngredientRequest.objects.filter(restaurant=restaurant)
    if active_branch:
        requests_qs = requests_qs.filter(branch=active_branch)

    if status_filter != "all":
        requests_qs = requests_qs.filter(status=status_filter.upper())

    requests_list = requests_qs.select_related("ingredient", "requested_by", "reviewed_by", "purchase_order").order_by("-created_at")
    all_ingredients = Ingredient.objects.filter(restaurant=restaurant).order_by("name")

    context = {
        "requests": requests_list,
        "status_filter": status_filter,
        "all_ingredients": all_ingredients,
        "priorities": IngredientRequest.Priority.choices,
        "active_branch": active_branch,
        "restaurant": restaurant,
        "active_tab": "ingredient_requests",
        "unread_notifications_count": StaffNotification.objects.filter(recipient=user, is_read=False).count(),
        "notifications": StaffNotification.objects.filter(recipient=user).order_by("-created_at")[:10],
    }
    return render(request, "kitchen/ingredient_requests.html", context)


@require_POST
@login_required(login_url="/auth/login/")
@kitchen_operator_required
def kitchen_cancel_ingredient_request(request, request_id):
    user, _, active_branch, restaurant = _get_kitchen_context(request)
    req = get_object_or_404(
        IngredientRequest,
        id=request_id,
        restaurant=restaurant,
    )

    if req.status != IngredientRequest.Status.PENDING:
        messages.error(request, "Only pending requests can be cancelled.")
        return redirect("kitchen_ingredient_requests")

    # Kitchen staff who requested it, or manager/admin, can cancel
    if req.requested_by_id != user.id and user.role not in ["chief", "kitchen_manager", "admin", "owner", "manager"]:
        raise PermissionDenied("You can only cancel your own pending requests.")

    req.delete()
    messages.success(request, f"Request #{request_id} has been cancelled.")
    return redirect("kitchen_ingredient_requests")


# ===========================================================================
# 7. KITCHEN PERFORMANCE
# ===========================================================================

@login_required(login_url="/auth/login/")
@kitchen_operator_required
@require_GET
def kitchen_performance(request):
    user, _, active_branch, restaurant = _get_kitchen_context(request)
    today = local_work_date()
    operating_settings = resolve_settings(restaurant, active_branch)
    warning_minutes = operating_settings.get("kitchen_warning_minutes") or 15

    today_orders_qs = Order.objects.filter(
        restaurant=restaurant,
        created_at__date=today,
    )
    if active_branch:
        today_orders_qs = today_orders_qs.filter(branch=active_branch)

    total_today = today_orders_qs.count()
    ready_or_completed = today_orders_qs.filter(status__in=["READY", "SERVED", "COMPLETED"]).count()
    in_kitchen = today_orders_qs.filter(status__in=["NEW", "ACCEPTED", "PREPARING"]).count()

    # Calculate average preparation duration
    durations = []
    completed_orders = today_orders_qs.filter(status__in=["READY", "SERVED", "COMPLETED"])
    for o in completed_orders:
        if o.status_changed_at and o.created_at:
            mins = (o.status_changed_at - o.created_at).total_seconds() / 60.0
            if 0 < mins < 300:
                durations.append(mins)

    avg_prep_time = round(sum(durations) / len(durations), 1) if durations else None

    # Calculate average waiter pickup duration
    pickup_durations = []
    ready_or_past_orders = today_orders_qs.filter(status__in=["READY", "SERVED", "COMPLETED"]).select_related("staff_service")
    for o in ready_or_past_orders:
        if hasattr(o, "staff_service") and o.staff_service and o.staff_service.ready_at and o.staff_service.served_at:
            dur = (o.staff_service.served_at - o.staff_service.ready_at).total_seconds() / 60.0
            if 0 < dur < 180:
                pickup_durations.append(dur)
    avg_pickup_time = round(sum(pickup_durations) / len(pickup_durations), 1) if pickup_durations else None

    # Count delayed orders
    delayed_count = 0
    now = timezone.now()
    for o in today_orders_qs.filter(status__in=["NEW", "ACCEPTED", "PREPARING"]):
        since = o.status_changed_at or o.created_at
        if (now - since).total_seconds() >= warning_minutes * 60:
            delayed_count += 1

    # Top items prepared today
    top_items = (
        OrderItem.objects.filter(order__in=today_orders_qs)
        .values("menu_item__name")
        .annotate(total_qty=Count("id"))
        .order_by("-total_qty")[:5]
    )

    context = {
        "total_today": total_today,
        "ready_or_completed": ready_or_completed,
        "in_kitchen": in_kitchen,
        "avg_prep_time": avg_prep_time,
        "avg_pickup_time": avg_pickup_time,
        "delayed_count": delayed_count,
        "top_items": top_items,
        "active_branch": active_branch,
        "restaurant": restaurant,
        "active_tab": "performance",
        "unread_notifications_count": StaffNotification.objects.filter(recipient=user, is_read=False).count(),
        "notifications": StaffNotification.objects.filter(recipient=user).order_by("-created_at")[:10],
    }
    return render(request, "kitchen/performance.html", context)


# ===========================================================================
# 8. EMPLOYEE SELF-SERVICE (KITCHEN STAFF)
# ===========================================================================

@login_required(login_url="/auth/login/")
@kitchen_operator_required
@require_GET
def kitchen_shift(request):
    user, profile, active_branch, restaurant = _get_kitchen_context(request)
    today = local_work_date()

    today_attendance = None
    if profile:
        today_attendance = Attendance.objects.filter(
            employee=profile,
            work_date=today,
        ).select_related("shift").first()

    current_shift = None
    if today_attendance and today_attendance.shift:
        current_shift = today_attendance.shift
    elif profile and profile.shift:
        current_shift = profile.shift

    shift_history = []
    if profile:
        shift_history = Attendance.objects.filter(
            employee=profile,
        ).select_related("shift").order_by("-work_date")[:14]

    all_shifts = Shift.objects.filter(restaurant=restaurant, is_active=True).order_by("start_time")

    context = {
        "profile": profile,
        "current_shift": current_shift,
        "today_attendance": today_attendance,
        "shift_history": shift_history,
        "all_shifts": all_shifts,
        "active_branch": active_branch,
        "restaurant": restaurant,
        "active_tab": "shift",
        "unread_notifications_count": StaffNotification.objects.filter(recipient=user, is_read=False).count(),
        "notifications": StaffNotification.objects.filter(recipient=user).order_by("-created_at")[:10],
    }
    return render(request, "kitchen/shift.html", context)


@login_required(login_url="/auth/login/")
@kitchen_operator_required
def kitchen_attendance(request):
    user, profile, active_branch, restaurant = _get_kitchen_context(request)
    now = timezone.now()
    today = local_work_date()

    today_record = None
    is_checked_in = False
    is_checked_out = False
    worked_hours_str = "0h 0m"
    overtime_hours_str = "0h 0m"

    if profile:
        today_record = Attendance.objects.filter(
            employee=profile,
            work_date=today,
        ).select_related("shift").order_by("-check_in").first()

        if today_record:
            if today_record.check_out:
                is_checked_out = True
                wm = today_record.worked_minutes
                h, m = divmod(wm, 60)
                worked_hours_str = f"{h}h {m}m"
                otm = today_record.overtime_minutes
                oth, otm_rem = divmod(otm, 60)
                overtime_hours_str = f"{oth}h {otm_rem}m"
            else:
                is_checked_in = True
                wm = today_record.get_worked_minutes(now)
                h, m = divmod(wm, 60)
                worked_hours_str = f"{h}h {m}m"

    history = []
    if profile:
        history = Attendance.objects.filter(
            employee=profile,
        ).select_related("shift").order_by("-work_date")[:30]

    context = {
        "profile": profile,
        "today_record": today_record,
        "is_checked_in": is_checked_in,
        "is_checked_out": is_checked_out,
        "worked_hours_str": worked_hours_str,
        "overtime_hours_str": overtime_hours_str,
        "history": history,
        "active_branch": active_branch,
        "restaurant": restaurant,
        "active_tab": "attendance",
        "unread_notifications_count": StaffNotification.objects.filter(recipient=user, is_read=False).count(),
        "notifications": StaffNotification.objects.filter(recipient=user).order_by("-created_at")[:10],
    }
    return render(request, "kitchen/attendance.html", context)


@login_required(login_url="/auth/login/")
@kitchen_operator_required
@require_POST
def kitchen_check_in(request):
    try:
        attendance = check_in_employee(request.user)
        messages.success(request, f"Checked in successfully at {timezone.localtime(attendance.check_in).strftime('%I:%M %p')}.")
    except ValidationError as err:
        messages.error(request, " ".join(err.messages if hasattr(err, "messages") else [str(err)]))
    return redirect("kitchen_attendance")


@login_required(login_url="/auth/login/")
@kitchen_operator_required
@require_POST
def kitchen_check_out(request):
    try:
        attendance = check_out_employee(request.user)
        messages.success(request, f"Checked out successfully at {timezone.localtime(attendance.check_out).strftime('%I:%M %p')}.")
    except ValidationError as err:
        messages.error(request, " ".join(err.messages if hasattr(err, "messages") else [str(err)]))
    return redirect("kitchen_attendance")


@login_required(login_url="/auth/login/")
@kitchen_operator_required
def kitchen_leave(request):
    user, profile, active_branch, restaurant = _get_kitchen_context(request)

    if request.method == "POST":
        leave_type = request.POST.get("leave_type")
        start_date_str = request.POST.get("start_date")
        end_date_str = request.POST.get("end_date")
        reason = request.POST.get("reason", "").strip()

        if not profile:
            messages.error(request, "Employee profile required to request leave.")
            return redirect("kitchen_leave")

        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
            if end_date < start_date:
                raise ValidationError("End date cannot be before start date.")
            if not reason:
                raise ValidationError("Please provide a reason for leave.")

            LeaveRequest.objects.create(
                employee=profile,
                restaurant=restaurant,
                branch=active_branch,
                leave_type=leave_type,
                start_date=start_date,
                end_date=end_date,
                reason=reason,
                status="pending",
            )
            messages.success(request, "Leave request submitted successfully.")
            return redirect("kitchen_leave")
        except (ValueError, ValidationError) as err:
            messages.error(request, str(err))

    leaves = []
    if profile:
        leaves = LeaveRequest.objects.filter(employee=profile).order_by("-created_at")

    context = {
        "profile": profile,
        "leaves": leaves,
        "leave_types": LeaveRequest.LEAVE_TYPE_CHOICES,
        "active_branch": active_branch,
        "restaurant": restaurant,
        "active_tab": "leave",
        "unread_notifications_count": StaffNotification.objects.filter(recipient=user, is_read=False).count(),
        "notifications": StaffNotification.objects.filter(recipient=user).order_by("-created_at")[:10],
    }
    return render(request, "kitchen/leave.html", context)


@login_required(login_url="/auth/login/")
@kitchen_operator_required
@require_GET
def kitchen_salary(request):
    user, profile, active_branch, restaurant = _get_kitchen_context(request)
    now = timezone.now()

    current_month_worked_hours = Decimal("0.00")
    current_month_ot_hours = Decimal("0.00")
    if profile:
        monthly_att = Attendance.objects.filter(
            employee=profile,
            work_date__year=now.year,
            work_date__month=now.month,
        ).select_related("shift")
        total_wm = sum(rec.worked_minutes for rec in monthly_att)
        total_otm = sum(rec.overtime_minutes for rec in monthly_att)
        current_month_worked_hours = Decimal(str(round(total_wm / 60.0, 2)))
        current_month_ot_hours = Decimal(str(round(total_otm / 60.0, 2)))

    basic_salary = profile.basic_salary if profile else Decimal("0.00")
    hourly_rate = getattr(profile, "hourly_rate", None) or (
        (basic_salary / Decimal("208")).quantize(Decimal("0.01")) if basic_salary else Decimal("0.00")
    )
    overtime_rate = (hourly_rate * Decimal("1.5")).quantize(Decimal("0.01")) if hourly_rate else Decimal("0.00")

    estimated_ot_pay = current_month_ot_hours * overtime_rate
    estimated_net = basic_salary + estimated_ot_pay

    context = {
        "profile": profile,
        "basic_salary": basic_salary,
        "hourly_rate": hourly_rate,
        "overtime_rate": overtime_rate,
        "worked_hours_month": current_month_worked_hours,
        "ot_hours_month": current_month_ot_hours,
        "estimated_ot_pay": estimated_ot_pay,
        "estimated_net": estimated_net,
        "active_branch": active_branch,
        "restaurant": restaurant,
        "active_tab": "salary",
        "unread_notifications_count": StaffNotification.objects.filter(recipient=user, is_read=False).count(),
        "notifications": StaffNotification.objects.filter(recipient=user).order_by("-created_at")[:10],
    }
    return render(request, "kitchen/salary.html", context)


@login_required(login_url="/auth/login/")
@kitchen_operator_required
def kitchen_salary_advance(request):
    user, profile, active_branch, restaurant = _get_kitchen_context(request)

    if request.method == "POST":
        amount_str = request.POST.get("amount", "").strip()
        reason = request.POST.get("reason", "").strip()

        if not profile:
            messages.error(request, "Employee profile required to request salary advance.")
            return redirect("kitchen_salary_advance")

        try:
            amount = Decimal(amount_str)
            if amount <= Decimal("0.00"):
                raise ValidationError("Amount must be greater than zero.")
            if not reason:
                raise ValidationError("Please specify the reason for advance.")

            SalaryAdvance.objects.create(
                employee=profile,
                restaurant=restaurant,
                branch=active_branch,
                amount=amount,
                reason=reason,
                status="pending",
            )
            messages.success(request, f"Salary advance request of ৳{amount} submitted.")
            return redirect("kitchen_salary_advance")
        except (ValueError, ValidationError) as err:
            messages.error(request, str(err))

    advances = []
    if profile:
        advances = SalaryAdvance.objects.filter(employee=profile).order_by("-created_at")

    context = {
        "profile": profile,
        "advances": advances,
        "active_branch": active_branch,
        "restaurant": restaurant,
        "active_tab": "salary_advance",
        "unread_notifications_count": StaffNotification.objects.filter(recipient=user, is_read=False).count(),
        "notifications": StaffNotification.objects.filter(recipient=user).order_by("-created_at")[:10],
    }
    return render(request, "kitchen/advance.html", context)


@login_required(login_url="/auth/login/")
@kitchen_operator_required
@require_GET
def kitchen_payroll_history(request):
    user, profile, active_branch, restaurant = _get_kitchen_context(request)

    records = []
    if profile:
        records = PayrollRecord.objects.filter(
            employee=profile
        ).order_by("-month")

    context = {
        "profile": profile,
        "records": records,
        "active_branch": active_branch,
        "restaurant": restaurant,
        "active_tab": "payroll_history",
        "unread_notifications_count": StaffNotification.objects.filter(recipient=user, is_read=False).count(),
        "notifications": StaffNotification.objects.filter(recipient=user).order_by("-created_at")[:10],
    }
    return render(request, "kitchen/payroll_history.html", context)


# ===========================================================================
# 9. NOTIFICATIONS
# ===========================================================================

@login_required(login_url="/auth/login/")
@kitchen_operator_required
@require_POST
def kitchen_mark_notification_read(request, notification_id):
    notif = get_object_or_404(StaffNotification, id=notification_id, recipient=request.user)
    notif.is_read = True
    notif.save(update_fields=["is_read"])
    return redirect(request.POST.get("next") or request.GET.get("next") or "kitchen_dashboard")


@login_required(login_url="/auth/login/")
@kitchen_operator_required
@require_POST
def kitchen_mark_all_notifications_read(request):
    StaffNotification.objects.filter(recipient=request.user, is_read=False).update(is_read=True)
    messages.success(request, "All notifications marked as read.")
    return redirect(request.POST.get("next") or request.GET.get("next") or "kitchen_dashboard")
