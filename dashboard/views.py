from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Avg, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from menu.models import Category
from orders.models import Order
from orders.services import orders_for_user, transition_order_status
from users.decorators import role_required


@role_required("admin", "owner", "manager", "waiter", "bar_manager")
def owner_dashboard(request):
    today = timezone.localtime()

    today_orders = orders_for_user(request.user).filter(
        created_at__date=today
    )

    today_revenue = today_orders.aggregate(
        total=Sum("total_amount")
    )["total"] or 0

    total_orders = today_orders.count()

    active_orders = orders_for_user(request.user).filter(
        status__in=[
            "NEW",
            "ACCEPTED",
            "PREPARING",
            "READY",
        ]
    ).count()

    average_order_value = today_orders.aggregate(
        average=Avg("total_amount")
    )["average"] or 0

    recent_orders = (
        orders_for_user(request.user).select_related("table")
        .order_by("-created_at")[:5]
    )

    context = {
        "today_revenue": today_revenue,
        "total_orders": total_orders,
        "active_orders": active_orders,
        "average_order_value": average_order_value,
        "recent_orders": recent_orders,
        "user_role": request.user.get_role_display(),
    }

    return render(
        request,
        "dashboard/index.html",
        context,
    )


def orders_list(request):
    orders = (
        orders_for_user(request.user).select_related(
            "restaurant",
            "table",
        )
        .order_by("-created_at")
    )

    search = request.GET.get("search", "").strip()
    status = request.GET.get("status", "").strip()
    order_type = request.GET.get("order_type", "").strip()

    if search:
        if search.isdigit():
            orders = orders.filter(id=int(search))
        else:
            orders = orders.filter(
                Q(restaurant__name__icontains=search)
            )

    if status:
        orders = orders.filter(status=status)

    if order_type:
        orders = orders.filter(order_type=order_type)

    paginator = Paginator(orders, 10)

    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    status_choices = [
        {
            "value": value,
            "label": label,
            "selected": value == status,
        }
        for value, label in Order.STATUS_CHOICES
    ]

    order_type_choices = [
        {
            "value": value,
            "label": label,
            "selected": value == order_type,
        }
        for value, label in Order.ORDER_TYPES
    ]

    context = {
        "page_obj": page_obj,
        "search": search,
        "selected_status": status,
        "selected_order_type": order_type,
        "status_choices": status_choices,
        "order_type_choices": order_type_choices,
    }

    return render(
        request,
        "dashboard/orders.html",
        context,
    )


def order_detail(request, order_id):
    order = get_object_or_404(
        orders_for_user(request.user).select_related(
            "restaurant",
            "table",
        ).prefetch_related(
            "items__menu_item"
        ),
        id=order_id,
    )

    owner_transitions = {
        "NEW": [("ACCEPTED", "Accept Order")],
        "ACCEPTED": [],
        "PREPARING": [],
        "READY": [("SERVED", "Mark Served")],
        "SERVED": [("COMPLETED", "Complete Order")],
        "COMPLETED": [],
    }

    if request.method == "POST":
        new_status = request.POST.get("status")

        try:
            transition_order_status(
                order,
                new_status,
                allowed_targets={"ACCEPTED", "SERVED", "COMPLETED"},
            )
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))

        return redirect(
            "order_detail",
            order_id=order.id,
        )

    context = {
        "order": order,
        "status_choices": owner_transitions.get(
            order.status,
            [],
        ),
    }

    return render(
        request,
        "dashboard/order_detail.html",
        context,
    )


def category_list(request):
    categories = (
        Category.objects.select_related("restaurant")
        .order_by("name")
    )

    return render(
        request,
        "dashboard/categories.html",
        {
            "categories": categories,
        },
    )


def category_create(request):
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        restaurant_id = request.POST.get("restaurant")

        if name and restaurant_id:
            Category.objects.create(
                name=name,
                restaurant_id=restaurant_id,
            )

            return redirect("category_list")

    from restaurant.models import Restaurant

    restaurants = [
        {
            "id": restaurant.id,
            "name": restaurant.name,
            "selected": False,
        }
        for restaurant in Restaurant.objects.all().order_by("name")
    ]

    return render(
        request,
        "dashboard/category_form.html",
        {
            "restaurants": restaurants,
            "page_title": "Add Category",
            "button_text": "Create Category",
        },
    )


def category_edit(request, category_id):
    category = get_object_or_404(
        Category,
        id=category_id,
    )

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        restaurant_id = request.POST.get("restaurant")

        if name and restaurant_id:
            category.name = name
            category.restaurant_id = restaurant_id
            category.save()

            return redirect("category_list")

    from restaurant.models import Restaurant

    restaurants = [
        {
            "id": restaurant.id,
            "name": restaurant.name,
            "selected": restaurant.id == category.restaurant_id,
        }
        for restaurant in Restaurant.objects.all().order_by("name")
    ]

    return render(
        request,
        "dashboard/category_form.html",
        {
            "category": category,
            "restaurants": restaurants,
            "page_title": "Edit Category",
            "button_text": "Save Changes",
        },
    )


def category_delete(request, category_id):
    category = get_object_or_404(
        Category,
        id=category_id,
    )

    if request.method == "POST":
        category.delete()

    return redirect("category_list")
