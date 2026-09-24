from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q, Sum
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from orders.models import Order
from orders.services import get_live_order_cutoff

from .forms import BranchForm, FloorForm, TableForm
from .models import Branch, Floor, Restaurant, Table


MANAGEMENT_ROLES = {
    "admin",
    "owner",
    "manager",
}


def restaurant_manager_required(view_function):
    @wraps(view_function)
    @login_required
    def wrapped_view(request, *args, **kwargs):
        user = request.user

        if user.is_superuser:
            return view_function(request, *args, **kwargs)

        if (
            not user.is_active
            or not getattr(user, "is_active_staff", False)
            or getattr(user, "role", None) not in MANAGEMENT_ROLES
        ):
            raise PermissionDenied

        return view_function(request, *args, **kwargs)

    return wrapped_view


def get_actor_restaurant(request):
    user = request.user

    if user.is_superuser:
        restaurant_id = (
            request.POST.get("restaurant_id")
            or request.GET.get("restaurant_id")
        )

        if restaurant_id:
            return get_object_or_404(
                Restaurant,
                pk=restaurant_id,
            )

        restaurant = Restaurant.objects.order_by("id").first()

        if restaurant is None:
            raise PermissionDenied(
                "No restaurant is available."
            )

        return restaurant

    restaurant_id = getattr(user, "restaurant_id", None)

    if not restaurant_id:
        raise PermissionDenied(
            "Your account is not assigned to a restaurant."
        )

    return get_object_or_404(
        Restaurant,
        pk=restaurant_id,
    )


@restaurant_manager_required
def floor_management(request):
    restaurant = get_actor_restaurant(request)

    if request.method == "POST":
        form = FloorForm(
            request.POST,
            restaurant=restaurant,
        )

        if form.is_valid():
            floor = form.save()
            messages.success(
                request,
                f"{floor.name} was created successfully.",
            )
            return redirect("restaurant:floor_management")
    else:
        form = FloorForm(restaurant=restaurant)

    floors = (
        Floor.objects.filter(restaurant=restaurant)
        .annotate(
            table_count=Count("tables"),
            active_table_count=Count(
                "tables",
                filter=Q(tables__is_active=True),
            ),
            total_capacity=Sum("tables__capacity"),
        )
        .order_by("floor_number", "name")
    )

    context = {
        "restaurant": restaurant,
        "floors": floors,
        "form": form,
        "current_url": "floor_management",
    }

    return render(
        request,
        "restaurant/floor_management.html",
        context,
    )


@restaurant_manager_required
@require_POST
def floor_update(request, floor_id):
    restaurant = get_actor_restaurant(request)

    floor = get_object_or_404(
        Floor,
        pk=floor_id,
        restaurant=restaurant,
    )

    form = FloorForm(
        request.POST,
        instance=floor,
        restaurant=restaurant,
    )

    if form.is_valid():
        form.save()
        messages.success(
            request,
            f"{floor.name} was updated successfully.",
        )
    else:
        error_message = next(
            iter(form.errors.values())
        )[0]

        messages.error(
            request,
            error_message,
        )

    return redirect("restaurant:floor_management")


@restaurant_manager_required
@require_POST
def floor_toggle(request, floor_id):
    restaurant = get_actor_restaurant(request)

    floor = get_object_or_404(
        Floor,
        pk=floor_id,
        restaurant=restaurant,
    )
    if (
        floor.is_active
        and floor.tables.filter(is_active=True).exists()
    ):
        messages.error(
            request,
            f"{floor.name} still has active tables. "
            "Deactivate or move those tables first.",
        )
        return redirect("restaurant:floor_management")

    floor.is_active = not floor.is_active
    floor.save(update_fields=["is_active"])

    state = "activated" if floor.is_active else "deactivated"

    messages.success(
        request,
        f"{floor.name} was {state}.",
    )

    return redirect("restaurant:floor_management")


@restaurant_manager_required
def table_management(request):
    restaurant = get_actor_restaurant(request)

    if request.method == "POST":
        form = TableForm(
            request.POST,
            restaurant=restaurant,
        )

        if form.is_valid():
            table = form.save()

            messages.success(
                request,
                f"Table {table.table_number} was created successfully.",
            )

            return redirect(
                "restaurant:table_management"
            )
    else:
        form = TableForm(restaurant=restaurant)

    floors = Floor.objects.filter(
        restaurant=restaurant,
    ).order_by(
        "floor_number",
        "name",
    )

    tables = Table.objects.filter(
        restaurant=restaurant,
    ).select_related(
        "floor",
        "branch",
    )

    branches = list(
        restaurant.branches.filter(is_active=True).order_by("-is_main", "name")
    )

    selected_branch = request.GET.get("branch")
    selected_floor = request.GET.get("floor")
    selected_status = request.GET.get("status")

    user = request.user
    if getattr(user, "role", None) == "manager" and getattr(user, "branch_id", None):
        tables = tables.filter(branch_id=user.branch_id)
        selected_branch = str(user.branch_id)
    elif selected_branch and selected_branch.isdigit():
        tables = tables.filter(branch_id=int(selected_branch))

    if selected_floor and selected_floor.isdigit():
        tables = tables.filter(
            floor_id=int(selected_floor)
        )

    valid_statuses = {
        choice[0] for choice in Table.STATUS_CHOICES
    }

    if selected_status not in valid_statuses:
        selected_status = ""
    tables = list(
        tables.order_by(
            "floor__floor_number",
            "table_number",
        )
    )

    live_cutoff = get_live_order_cutoff()
    table_ids = [table.id for table in tables]

    active_orders = (
        Order.objects.filter(
            restaurant=restaurant,
            table_id__in=table_ids,
            created_at__gte=live_cutoff,
        )
        .filter(
            Q(table_session__status="OPEN")
            | Q(table_session__isnull=True)
        )
        .exclude(status__in=["COMPLETED", "CANCELLED"])
        .order_by("table_id", "-created_at")
    )

    latest_order_by_table = {}

    for order in active_orders:
        latest_order_by_table.setdefault(
            order.table_id,
            order,
        )

    unpaid_tables = set(
        Order.objects.filter(
            restaurant=restaurant,
            table_id__in=table_ids,
            payment_status="UNPAID",
            created_at__gte=live_cutoff,
        )
        .filter(
            Q(table_session__status="OPEN")
            | Q(table_session__isnull=True)
        )
        .exclude(status__in=["COMPLETED", "CANCELLED"])
        .values_list("table_id", flat=True)
    )

    service_orders_by_table = set(
        Order.objects.filter(
            restaurant=restaurant,
            table_id__in=table_ids,
            created_at__gte=live_cutoff,
        )
        .filter(
            Q(table_session__status="OPEN")
            | Q(table_session__isnull=True)
        )
        .exclude(status="CANCELLED")
        .values_list("table_id", flat=True)
    )

    stale_order_tables = set(
        Order.objects.filter(
            restaurant=restaurant,
            table_id__in=table_ids,
            created_at__lt=live_cutoff,
        )
        .values_list("table_id", flat=True)
    )

    waiting_count = 0
    serving_count = 0
    available_count = 0
    occupied_count = 0

    from django.core.exceptions import ImproperlyConfigured
    qr_error = None
    for table in tables:
        try:
            table.generate_qr_code()
        except ImproperlyConfigured as error:
            if qr_error is None:
                qr_error = str(error)
                messages.error(request, qr_error)
        except Exception:
            pass
        table.menu_url = table.safe_menu_url
        has_live_service = table.id in service_orders_by_table
        has_stale_orders = table.id in stale_order_tables
        if table.status == Table.STATUS_OCCUPIED and not has_live_service and has_stale_orders:
            table.close_service()

        active_order = latest_order_by_table.get(table.id)
        table.active_order = active_order
        table.has_unpaid_orders = table.id in unpaid_tables
        table.display_status = table.status

        if active_order:
            if active_order.status == "NEW":
                table.display_status = Table.STATUS_WAITING
            elif active_order.status in {
                "ACCEPTED",
                "PREPARING",
                "READY",
                "SERVED",
            }:
                table.display_status = Table.STATUS_SERVING
        elif table.status == Table.STATUS_OCCUPIED:
            table.display_status = Table.STATUS_OCCUPIED
        else:
            table.display_status = table.status

        if not table.is_active:
            table.display_status = Table.STATUS_OUT_OF_SERVICE

        table.can_close_service = (
            table.display_status in {Table.STATUS_WAITING, Table.STATUS_SERVING, Table.STATUS_OCCUPIED}
        ) and not table.has_unpaid_orders

        if table.display_status == Table.STATUS_WAITING:
            waiting_count += 1
        elif table.display_status == Table.STATUS_SERVING:
            serving_count += 1
        elif table.display_status == Table.STATUS_OCCUPIED:
            occupied_count += 1
        elif table.display_status == Table.STATUS_AVAILABLE:
            available_count += 1

    summary = {
        "total": len(tables),
        "available": available_count,
        "waiting": waiting_count,
        "serving": serving_count,
        "occupied": occupied_count,
    }
    if selected_status:
        tables = [
            table
            for table in tables
            if table.display_status == selected_status
        ]

    context = {
        "restaurant": restaurant,
        "branches": branches,
        "floors": floors,
        "tables": tables,
        "form": form,
        "summary": summary,
        "selected_floor": selected_floor or "",
        "selected_branch": selected_branch or "",
        "selected_status": selected_status or "",
        "status_choices": Table.STATUS_CHOICES,
        "current_url": "table_management",
    }

    return render(
        request,
        "restaurant/table_management.html",
        context,
    )


@restaurant_manager_required
@require_POST
def table_update(request, table_id):
    restaurant = get_actor_restaurant(request)

    table = get_object_or_404(
        Table,
        pk=table_id,
        restaurant=restaurant,
    )

    user = request.user
    if getattr(user, "role", None) == "manager" and getattr(user, "branch_id", None):
        if table.branch_id and table.branch_id != user.branch_id:
            raise PermissionDenied

    form = TableForm(
        request.POST,
        instance=table,
        restaurant=restaurant,
    )

    if form.is_valid():
        active_order_exists = (
            Order.objects.filter(
                restaurant=restaurant,
                table=table,
            )
            .exclude(status="COMPLETED")
            .exists()
        )

        protected_fields = {
            "floor",
            "table_number",
            "status",
            "is_active",
        }

        protected_changes = (
            protected_fields.intersection(
                form.changed_data
            )
        )

        if active_order_exists and protected_changes:
            messages.error(
                request,
                "This table has an active order. Its floor, number, "
                "status or active state cannot be changed.",
            )
        else:
            table = form.save()

            messages.success(
                request,
                f"Table {table.table_number} was updated successfully.",
            )
    else:
        error_message = next(
            iter(form.errors.values())
        )[0]

        messages.error(
            request,
            error_message,
        )

    return redirect("restaurant:table_management")


@restaurant_manager_required
@require_POST
def table_toggle(request, table_id):
    restaurant = get_actor_restaurant(request)

    table = get_object_or_404(
        Table,
        pk=table_id,
        restaurant=restaurant,
    )

    user = request.user
    if getattr(user, "role", None) == "manager" and getattr(user, "branch_id", None):
        if table.branch_id and table.branch_id != user.branch_id:
            raise PermissionDenied

    active_order_exists = (
        Order.objects.filter(
            restaurant=restaurant,
            table=table,
        )
        .exclude(status="COMPLETED")
        .exists()
    )

    if table.is_active and active_order_exists:
        messages.error(
            request,
            f"Table {table.table_number} has an active order "
            "and cannot be deactivated.",
        )
        return redirect("restaurant:table_management")

    table.is_active = not table.is_active

    if table.is_active:
        table.status = Table.STATUS_AVAILABLE
    else:
        table.status = Table.STATUS_OUT_OF_SERVICE

    table.save(
        update_fields=[
            "is_active",
            "status",
        ]
    )

    state = "activated" if table.is_active else "deactivated"

    messages.success(
        request,
        f"Table {table.table_number} was {state}.",
    )

    return redirect("restaurant:table_management")


@restaurant_manager_required
def qr_view(request, table_id):
    restaurant = get_actor_restaurant(request)

    table = get_object_or_404(
        Table.objects.select_related("floor", "branch"),
        pk=table_id,
        restaurant=restaurant,
    )

    user = request.user
    if getattr(user, "role", None) == "manager" and getattr(user, "branch_id", None):
        if table.branch_id and table.branch_id != user.branch_id:
            raise PermissionDenied

    from django.core.exceptions import ImproperlyConfigured
    try:
        table.generate_qr_code()
    except ImproperlyConfigured as error:
        if (
            request.headers.get("x-requested-with") == "XMLHttpRequest"
            or "application/json" in request.headers.get("Accept", "")
            or request.GET.get("format") == "json"
        ):
            return JsonResponse({"success": False, "error": str(error)}, status=400)
        messages.error(request, str(error))
        return redirect("restaurant:table_management")

    branch_name = table.branch.name if table.branch else ""
    menu_url = table.safe_menu_url
    qr_url = table.qr_code.url if table.qr_code else ""

    if (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("Accept", "")
        or request.GET.get("format") == "json"
    ):
        return JsonResponse({
            "success": True,
            "table_id": table.id,
            "table_number": table.table_number,
            "branch": branch_name,
            "qr_url": qr_url,
            "menu_url": menu_url,
            "download_url": reverse("restaurant:qr_download", args=[table.id]),
            "print_url": reverse("restaurant:qr_print", args=[table.id]),
        })

    return render(
        request,
        "restaurant/qr_view.html",
        {
            "restaurant": restaurant,
            "table": table,
            "branch_name": branch_name,
            "menu_url": menu_url,
            "qr_url": qr_url,
        },
    )


@restaurant_manager_required
def qr_download(request, table_id):
    restaurant = get_actor_restaurant(request)

    table = get_object_or_404(
        Table,
        pk=table_id,
        restaurant=restaurant,
    )

    user = request.user
    if getattr(user, "role", None) == "manager" and getattr(user, "branch_id", None):
        if table.branch_id and table.branch_id != user.branch_id:
            raise PermissionDenied

    from django.core.exceptions import ImproperlyConfigured
    try:
        table.generate_qr_code()
    except ImproperlyConfigured as error:
        messages.error(request, str(error))
        return redirect("restaurant:table_management")

    return FileResponse(
        table.qr_code.open("rb"),
        as_attachment=True,
        filename=f"table-{table.table_number}-qr.png",
    )


@restaurant_manager_required
def qr_print(request, table_id):
    restaurant = get_actor_restaurant(request)

    table = get_object_or_404(
        Table.objects.select_related("floor", "branch"),
        pk=table_id,
        restaurant=restaurant,
    )

    user = request.user
    if getattr(user, "role", None) == "manager" and getattr(user, "branch_id", None):
        if table.branch_id and table.branch_id != user.branch_id:
            raise PermissionDenied

    from django.core.exceptions import ImproperlyConfigured
    try:
        table.generate_qr_code()
    except ImproperlyConfigured as error:
        messages.error(request, str(error))
        return redirect("restaurant:table_management")

    return render(
        request,
        "restaurant/qr_print.html",
        {
            "restaurant": restaurant,
            "table": table,
        },
    )


@restaurant_manager_required
@require_POST
def table_close_service(request, table_id):
    restaurant = get_actor_restaurant(request)
    table = get_object_or_404(
        Table,
        pk=table_id,
        restaurant=restaurant,
    )

    session = table.active_session
    live_cutoff = get_live_order_cutoff()
    if session:
        unpaid = session.orders.filter(
            payment_status="UNPAID",
            created_at__gte=live_cutoff,
        ).exclude(status__in=["COMPLETED", "CANCELLED"])
    else:
        unpaid = Order.objects.filter(
            table=table,
            payment_status="UNPAID",
            created_at__gte=live_cutoff,
        ).exclude(status__in=["COMPLETED", "CANCELLED"])

    if unpaid.exists():
        order_list = ", ".join(f"#{o.id}" for o in unpaid[:3])
        messages.error(
            request,
            f"Table {table.table_number} cannot be closed because it has unpaid orders ({order_list}). Please collect payment first.",
        )
        return redirect("restaurant:table_management")

    table.close_service()
    messages.success(
        request,
        f"Table {table.table_number} dining service has been closed and is now Available.",
    )
    return redirect("restaurant:table_management")


# ============================================================
# OWNER-ONLY GUARD
# ============================================================

def owner_required(view_function):
    """Restrict view to owners and superusers only."""
    @wraps(view_function)
    @login_required
    def wrapped_view(request, *args, **kwargs):
        user = request.user
        if user.is_superuser:
            return view_function(request, *args, **kwargs)
        if not user.is_active or getattr(user, "role", None) not in ("owner",):
            raise PermissionDenied
        return view_function(request, *args, **kwargs)
    return wrapped_view


# ============================================================
# BRANCH MANAGEMENT
# ============================================================

@owner_required
def branch_list(request):
    restaurant = get_actor_restaurant(request)
    branches = (
        restaurant.branches
        .annotate(
            floor_count=Count("floors", distinct=True),
            table_count=Count("tables", distinct=True),
        )
        .order_by("-is_main", "name")
    )
    return render(
        request,
        "restaurant/branch_management.html",
        {
            "restaurant": restaurant,
            "branches": branches,
            "current_url": "branch_list",
        },
    )


@owner_required
def branch_create(request):
    restaurant = get_actor_restaurant(request)
    if request.method == "POST":
        form = BranchForm(request.POST, restaurant=restaurant)
        if form.is_valid():
            branch = form.save()
            messages.success(request, f"Branch '{branch.name}' created successfully.")
            return redirect("restaurant:branch_list")
    else:
        form = BranchForm(restaurant=restaurant)
    return render(
        request,
        "restaurant/branch_form.html",
        {
            "restaurant": restaurant,
            "form": form,
            "form_title": "Add New Branch",
            "submit_label": "Create Branch",
            "current_url": "branch_create",
        },
    )


@owner_required
def branch_edit(request, branch_id):
    restaurant = get_actor_restaurant(request)
    branch = get_object_or_404(Branch, pk=branch_id, restaurant=restaurant)
    if request.method == "POST":
        form = BranchForm(request.POST, instance=branch, restaurant=restaurant)
        if form.is_valid():
            form.save()
            messages.success(request, f"Branch '{branch.name}' updated successfully.")
            return redirect("restaurant:branch_list")
    else:
        form = BranchForm(instance=branch, restaurant=restaurant)
    return render(
        request,
        "restaurant/branch_form.html",
        {
            "restaurant": restaurant,
            "form": form,
            "branch": branch,
            "form_title": f"Edit Branch — {branch.name}",
            "submit_label": "Save Changes",
            "current_url": "branch_edit",
        },
    )


@owner_required
@require_POST
def branch_toggle(request, branch_id):
    """Activate or deactivate a branch. Main branch cannot be deactivated."""
    restaurant = get_actor_restaurant(request)
    branch = get_object_or_404(Branch, pk=branch_id, restaurant=restaurant)

    if branch.is_active and branch.is_main:
        messages.error(request, "The Main Branch cannot be deactivated. Set another branch as main first.")
        return redirect("restaurant:branch_list")

    if branch.is_active:
        # Check for active orders before deactivating
        live_cutoff = get_live_order_cutoff()
        live_orders = Order.objects.filter(
            branch=branch,
            created_at__gte=live_cutoff,
        ).exclude(status__in=["COMPLETED", "CANCELLED"]).exists()
        if live_orders:
            messages.error(
                request,
                f"Branch '{branch.name}' has active orders and cannot be deactivated.",
            )
            return redirect("restaurant:branch_list")

    branch.is_active = not branch.is_active
    branch.save(update_fields=["is_active"])

    # If we just deactivated the active branch in the owner's session, clear it
    if not branch.is_active:
        if request.session.get("active_branch_id") == branch.id:
            del request.session["active_branch_id"]

    state = "activated" if branch.is_active else "deactivated"
    messages.success(request, f"Branch '{branch.name}' was {state}.")
    return redirect("restaurant:branch_list")


@owner_required
@require_POST
def branch_delete(request, branch_id):
    """Delete a branch. Protected if it is the Main Branch or has any historical orders."""
    restaurant = get_actor_restaurant(request)
    branch = get_object_or_404(Branch, pk=branch_id, restaurant=restaurant)

    if branch.is_main:
        messages.error(request, "The Main Branch cannot be deleted.")
        return redirect("restaurant:branch_list")

    # Protect against deletion if the branch has any orders (historical or active)
    if Order.objects.filter(branch=branch).exists():
        messages.error(
            request,
            f"Branch '{branch.name}' has order history and cannot be deleted. Deactivate it instead.",
        )
        return redirect("restaurant:branch_list")

    branch_name = branch.name
    branch.delete()
    messages.success(request, f"Branch '{branch_name}' was permanently deleted.")

    # Clear session if deleted branch was active
    if request.session.get("active_branch_id") == branch_id:
        del request.session["active_branch_id"]

    return redirect("restaurant:branch_list")


@require_POST
@login_required
def switch_branch(request):
    """POST endpoint to switch the active branch stored in the session.

    Owners and admins only. After switching, redirects to the ``next``
    parameter (the page the user was on) or falls back to branch_list.
    """
    user = request.user
    if not (getattr(user, "role", None) in ("owner", "admin") or user.is_superuser):
        raise PermissionDenied

    from .branch_services import set_active_branch

    try:
        branch_id = int(request.POST.get("branch_id", 0))
    except (TypeError, ValueError):
        messages.error(request, "Invalid branch ID.")
        return redirect("restaurant:branch_list")

    branch = set_active_branch(request, branch_id)
    if not branch:
        messages.error(request, "Branch not found or inactive.")
        return redirect("restaurant:branch_list")

    messages.success(request, f"Switched to branch: {branch.name}")

    next_url = request.POST.get("next", "").strip()
    # Basic open-redirect guard: only allow relative paths
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return redirect(next_url)
    return redirect("restaurant:branch_list")