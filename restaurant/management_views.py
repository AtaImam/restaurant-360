from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q, Sum
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from orders.models import Order

from .forms import FloorForm, TableForm
from .models import Floor, Restaurant, Table


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
    )

    selected_floor = request.GET.get("floor")
    selected_status = request.GET.get("status")

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

    table_ids = [table.id for table in tables]

    active_orders = (
        Order.objects.filter(
            restaurant=restaurant,
            table_id__in=table_ids,
        )
        .exclude(status="COMPLETED")
        .order_by("table_id", "-created_at")
    )

    latest_order_by_table = {}

    for order in active_orders:
        latest_order_by_table.setdefault(
            order.table_id,
            order,
        )

    waiting_count = 0
    serving_count = 0
    available_count = 0

    for table in tables:
        active_order = latest_order_by_table.get(table.id)
        table.active_order = active_order
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

        if not table.is_active:
            table.display_status = Table.STATUS_OUT_OF_SERVICE

        if table.display_status == Table.STATUS_WAITING:
            waiting_count += 1
        elif table.display_status == Table.STATUS_SERVING:
            serving_count += 1
        elif table.display_status == Table.STATUS_AVAILABLE:
            available_count += 1

    summary = {
        "total": len(tables),
        "available": available_count,
        "waiting": waiting_count,
        "serving": serving_count,
    }
    if selected_status:
        tables = [
            table
            for table in tables
            if table.display_status == selected_status
        ]

    context = {
        "restaurant": restaurant,
        "floors": floors,
        "tables": tables,
        "form": form,
        "summary": summary,
        "selected_floor": selected_floor or "",
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
def qr_download(request, table_id):
    restaurant = get_actor_restaurant(request)

    table = get_object_or_404(
        Table,
        pk=table_id,
        restaurant=restaurant,
    )

    if not table.qr_code:
        table.save()

    return FileResponse(
        table.qr_code.open("rb"),
        as_attachment=True,
        filename=f"table-{table.table_number}-qr.png",
    )


@restaurant_manager_required
def qr_print(request, table_id):
    restaurant = get_actor_restaurant(request)

    table = get_object_or_404(
        Table.objects.select_related("floor"),
        pk=table_id,
        restaurant=restaurant,
    )

    if not table.qr_code:
        table.save()

    return render(
        request,
        "restaurant/qr_print.html",
        {
            "restaurant": restaurant,
            "table": table,
        },
    )