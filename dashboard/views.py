from datetime import datetime, time
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Avg, Count, Prefetch, Q, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_safe

from inventory.models import StockReservation, StockTransaction
from menu.models import Category
from orders.models import Order, PaymentTransaction
from orders.services import (
    LIVE_STATUSES,
    STALE_AFTER_HOURS,
    archive_stale_order,
    get_live_order_cutoff,
    orders_for_user,
    transition_order_status,
)
from users.decorators import role_required


NEW_DELAY_MINUTES = 10
ACCEPTED_DELAY_MINUTES = 15
PREPARING_DELAY_MINUTES = 30
READY_DELAY_MINUTES = 5


def _live_order_cutoff(now):
    return get_live_order_cutoff(now)


def _order_age_metadata(order, now, delayed_after=None):
    # The stale review screen continues to show total order age.
    stage_started_at = (
        order.created_at if order.status == "NEW" else order.status_changed_at
    )
    started_at = (stage_started_at if delayed_after is not None else None) or order.created_at
    age_seconds = max((now - started_at).total_seconds(), 0)
    age_minutes = int(age_seconds // 60)
    order.age_minutes = age_minutes

    local_started = timezone.localtime(started_at)
    local_now = timezone.localtime(now)
    days_diff = (local_now.date() - local_started.date()).days
    hours = age_minutes // 60
    remaining_minutes = age_minutes % 60

    if days_diff == 0:
        if age_minutes < 1:
            order.age_label = "Just now"
        elif age_minutes < 60:
            order.age_label = f"{age_minutes} min"
        else:
            if remaining_minutes:
                order.age_label = f"{hours} hr {remaining_minutes} min"
            else:
                order.age_label = f"{hours} hr"
    elif days_diff == 1:
        order.age_label = "Yesterday"
    else:
        order.age_label = f"{days_diff} days ago"

    duration = order.age_label if age_minutes else "less than 1 min"
    if stage_started_at is None:
        if days_diff == 0:
            order.timing_label = (
                f"Received {duration} ago (stage timing unavailable)"
                if age_minutes
                else "Received just now (stage timing unavailable)"
            )
        elif days_diff == 1:
            order.timing_label = "Received Yesterday (stage timing unavailable)"
        else:
            order.timing_label = f"Received {days_diff} days ago (stage timing unavailable)"
    elif order.status == "READY":
        if days_diff == 0:
            order.timing_label = f"Ready {duration} ago" if age_minutes else "Ready just now"
        elif days_diff == 1:
            order.timing_label = "Ready since Yesterday"
        else:
            order.timing_label = f"Ready {days_diff} days ago"
    else:
        activity = "Cooking" if order.status == "PREPARING" else "Waiting"
        if days_diff == 0:
            order.timing_label = f"{activity} {duration}"
        elif days_diff == 1:
            order.timing_label = f"{activity} since Yesterday"
        else:
            order.timing_label = f"{activity} ({days_diff} days ago)"

    needs_attention = (
        order.status in LIVE_STATUSES
        and order.created_at >= get_live_order_cutoff(now)
        and delayed_after is not None
        and age_minutes >= delayed_after
    )
    order.is_delayed = needs_attention and order.status != "READY"
    order.is_urgent = needs_attention and order.status == "READY"
    return order


def _status_choices(selected_status):
    return [
        {
            "value": value,
            "label": label,
            "selected": value == selected_status,
        }
        for value, label in Order.STATUS_CHOICES
    ]


def _order_type_choices(selected_order_type):
    return [
        {
            "value": value,
            "label": label,
            "selected": value == selected_order_type,
        }
        for value, label in Order.ORDER_TYPES
    ]


def _apply_order_filters(queryset, request, *, allow_today=True):
    search = request.GET.get("search", "").strip()
    status = request.GET.get("status", "").strip()
    order_type = request.GET.get("order_type", "").strip()
    today_only = allow_today and request.GET.get("today", "") == "1"

    if search:
        if search.isdigit():
            number = int(search)
            queryset = queryset.filter(
                Q(id=number) | Q(table__table_number=number)
            )
        else:
            queryset = queryset.filter(
                Q(restaurant__name__icontains=search)
            )

    if status:
        queryset = queryset.filter(status=status)

    if order_type:
        queryset = queryset.filter(order_type=order_type)

    if today_only:
        queryset = queryset.filter(created_at__date=timezone.localdate())

    return queryset, search, status, order_type, today_only


from django.http import JsonResponse
from .analytics_services import get_dashboard_analytics


@role_required("admin", "owner", "manager", "waiter", "chief", "kitchen_manager", "bar_manager")
def owner_dashboard(request):
    if request.user.role == "waiter":
        return redirect("waiter:dashboard")
    if request.user.role in ["chief", "kitchen_manager"]:
        return redirect("/kitchen/")

    period = request.GET.get("period", "7d")
    analytics_data = get_dashboard_analytics(request, period=period)

    # Scoped recent orders (branch-aware or consolidated)
    user_orders = orders_for_user(request.user)
    if not analytics_data["branch_context"]["is_consolidated"] and analytics_data["branch_context"]["branch_id"]:
        user_orders = user_orders.filter(branch_id=analytics_data["branch_context"]["branch_id"])

    recent_orders = (
        user_orders
        .select_related("table")
        .order_by("-created_at")[:5]
    )

    kpi = analytics_data["kpis"]

    return render(
        request,
        "dashboard/index.html",
        {
            "analytics": analytics_data,
            "recent_orders": recent_orders,
            "user_role": request.user.get_role_display(),
            # Legacy context variables preserved for backward compatibility
            "today_revenue": Decimal(str(kpi["today_revenue"])),
            "total_orders": kpi["today_orders"],
            "active_orders": kpi["active_orders"],
            "average_order_value": Decimal(str(kpi["average_order_value"])),
        },
    )


@require_safe
@role_required("admin", "owner", "manager", "bar_manager")
def dashboard_analytics_api(request):
    """API endpoint providing real-time dashboard analytics for 60s polling and period filtering."""
    period = request.GET.get("period", "7d")
    data = get_dashboard_analytics(request, period=period)
    return JsonResponse(data)


@login_required(login_url='/auth/login/')
def orders_list(request):
    if request.user.role in ["chief", "kitchen_manager"]:
        return redirect("kitchen_order_queue")

    now = timezone.now()
    today = timezone.localdate(now)
    stale_cutoff = _live_order_cutoff(now)

    base_orders = orders_for_user(request.user).select_related(
        "restaurant",
        "table",
    )

    live_active = base_orders.filter(
        status__in=LIVE_STATUSES,
        created_at__gte=stale_cutoff,
    ).annotate(stage_started_at=Coalesce("status_changed_at", "created_at"))

    stale_active = base_orders.filter(
        status__in=LIVE_STATUSES,
        created_at__lt=stale_cutoff,
    )

    new_count = live_active.filter(status="NEW").count()
    accepted_count = live_active.filter(status="ACCEPTED").count()
    preparing_count = live_active.filter(status="PREPARING").count()
    ready_count = live_active.filter(status="READY").count()
    stale_count = stale_active.count()
    completed_today_count = base_orders.filter(
        status="COMPLETED",
        created_at__date=today,
    ).count()

    new_orders = [
        _order_age_metadata(order, now, NEW_DELAY_MINUTES)
        for order in (
            live_active.filter(status="NEW")
            .prefetch_related("items")
            .order_by("created_at")[:4]
        )
    ]

    ready_orders = [
        _order_age_metadata(order, now, READY_DELAY_MINUTES)
        for order in (
            live_active.filter(status="READY")
            .select_related("table", "staff_service__waiter__user")
            .prefetch_related("items")
            .order_by("stage_started_at")[:4]
        )
    ]

    accepted_attention_qs = live_active.filter(
        status="ACCEPTED",
        stage_started_at__lte=now - timezone.timedelta(
            minutes=ACCEPTED_DELAY_MINUTES
        ),
    )
    preparing_attention_qs = live_active.filter(
        status="PREPARING",
        stage_started_at__lte=now - timezone.timedelta(
            minutes=PREPARING_DELAY_MINUTES
        ),
    )

    accepted_attention = [
        _order_age_metadata(order, now, ACCEPTED_DELAY_MINUTES)
        for order in (
            accepted_attention_qs.prefetch_related("items")
            .order_by("stage_started_at")[:3]
        )
    ]

    preparing_attention = [
        _order_age_metadata(order, now, PREPARING_DELAY_MINUTES)
        for order in (
            preparing_attention_qs.prefetch_related("items")
            .order_by("stage_started_at")[:3]
        )
    ]

    attention_count = (
        new_count
        + ready_count
        + accepted_attention_qs.count()
        + preparing_attention_qs.count()
    )

    # Collect Payment / Payment Due for served PAY_LATER orders
    served_unpaid_qs = (
        base_orders.filter(
            status="SERVED",
            payment_status="UNPAID",
        )
        .select_related("table", "table_session", "restaurant")
        .prefetch_related("items__menu_item")
        .order_by("created_at")
    )

    payment_due_tables = []
    seen_groups = set()

    for s_order in served_unpaid_qs:
        session = s_order.table_session
        table = s_order.table

        group_key = f"session_{session.id}" if session else (f"table_{table.id}" if table else f"order_{s_order.id}")
        if group_key in seen_groups:
            continue
        seen_groups.add(group_key)

        if session:
            all_sess_orders = list(
                session.orders.exclude(status="CANCELLED")
                .prefetch_related("items__menu_item")
                .order_by("created_at")
            )
        elif table:
            all_sess_orders = list(
                base_orders.filter(
                    table=table,
                    created_at__gte=stale_cutoff,
                )
                .exclude(status="CANCELLED")
                .prefetch_related("items__menu_item")
                .order_by("created_at")
            )
        else:
            all_sess_orders = [s_order]

        unpaid_orders = [o for o in all_sess_orders if o.payment_status != "PAID"]
        paid_orders = [o for o in all_sess_orders if o.payment_status == "PAID"]
        unpaid_amount = sum((o.total_amount for o in unpaid_orders), Decimal("0.00"))
        total_amount = sum((o.total_amount for o in all_sess_orders), Decimal("0.00"))

        payment_due_tables.append({
            "group_key": group_key,
            "table": table,
            "session": session,
            "primary_order": s_order,
            "orders": all_sess_orders,
            "unpaid_orders": unpaid_orders,
            "paid_orders": paid_orders,
            "unpaid_amount": unpaid_amount,
            "total_amount": total_amount,
            "orders_count": len(all_sess_orders),
            "unpaid_count": len(unpaid_orders),
        })

    payment_due_count = len(payment_due_tables)

    # Normal Orders page intentionally hides stale ACTIVE orders. They have a
    # dedicated review screen so old demo/stuck orders never dominate live work.
    orders = base_orders.exclude(
        status__in=LIVE_STATUSES,
        created_at__lt=stale_cutoff,
    ).order_by("-created_at")

    orders, search, status, order_type, today_only = _apply_order_filters(
        orders,
        request,
    )

    page_obj = Paginator(orders, 10).get_page(request.GET.get("page"))
    thresholds = {
        "NEW": NEW_DELAY_MINUTES,
        "ACCEPTED": ACCEPTED_DELAY_MINUTES,
        "PREPARING": PREPARING_DELAY_MINUTES,
        "READY": READY_DELAY_MINUTES,
    }
    for order in page_obj.object_list:
        _order_age_metadata(order, now, thresholds.get(order.status))

    return render(
        request,
        "dashboard/orders.html",
        {
            "page_obj": page_obj,
            "search": search,
            "selected_status": status,
            "selected_order_type": order_type,
            "today_only": today_only,
            "status_choices": _status_choices(status),
            "order_type_choices": _order_type_choices(order_type),
            "new_count": new_count,
            "accepted_count": accepted_count,
            "preparing_count": preparing_count,
            "ready_count": ready_count,
            "completed_today_count": completed_today_count,
            "stale_count": stale_count,
            "new_orders": new_orders,
            "ready_orders": ready_orders,
            "accepted_attention": accepted_attention,
            "preparing_attention": preparing_attention,
            "attention_count": attention_count,
            "payment_due_tables": payment_due_tables,
            "payment_due_count": payment_due_count,
        },
    )



@require_safe
@login_required(login_url='/auth/login/')
def stale_orders_list(request):
    now = timezone.now()
    stale_cutoff = _live_order_cutoff(now)

    orders = (
        orders_for_user(request.user)
        .select_related("restaurant", "table")
        .filter(
            status__in=LIVE_STATUSES,
            created_at__lt=stale_cutoff,
        )
        .annotate(consumption_count=Count(
            "stock_transactions",
            filter=Q(stock_transactions__transaction_type=StockTransaction.TransactionType.CONSUMPTION),
        ))
        .prefetch_related(Prefetch(
            "stock_reservations",
            queryset=StockReservation.objects.select_related("ingredient").order_by("ingredient__name"),
            to_attr="review_reservations",
        ))
        .order_by("created_at")
    )

    orders, search, status, order_type, _ = _apply_order_filters(
        orders,
        request,
        allow_today=False,
    )

    page_obj = Paginator(orders, 15).get_page(request.GET.get("page"))

    for order in page_obj.object_list:
        _order_age_metadata(order, now)
        days, remaining_minutes = divmod(order.age_minutes, 1440)
        hours, minutes = divmod(remaining_minutes, 60)
        if days:
            order.review_age_label = f"{days}d {hours}h {minutes}m"
        elif hours and minutes:
            order.review_age_label = f"{hours}h {minutes}m"
        elif hours:
            order.review_age_label = f"{hours}h"
        elif minutes:
            order.review_age_label = f"{minutes}m"
        else:
            order.review_age_label = order.age_label
        reasons = []
        if timezone.localdate(order.created_at) < timezone.localdate(now):
            reasons.append("Unfinished order from a previous day")
        if order.created_at < now - timezone.timedelta(hours=STALE_AFTER_HOURS):
            reasons.append(f"Open for more than {STALE_AFTER_HOURS} hours")
        order.flag_reason = "; ".join(reasons)

    can_archive = request.user.is_authenticated and (
        request.user.is_superuser
        or getattr(request.user, "role", None) in {"admin", "owner", "manager"}
    )

    return render(
        request,
        "dashboard/stale_orders.html",
        {
            "page_obj": page_obj,
            "search": search,
            "selected_status": status,
            "selected_order_type": order_type,
            "status_choices": _status_choices(status),
            "order_type_choices": _order_type_choices(order_type),
            "stale_after_hours": STALE_AFTER_HOURS,
            "can_archive": can_archive,
        },
    )


@login_required(login_url='/auth/login/')
def order_detail(request, order_id):
    order = get_object_or_404(
        orders_for_user(request.user)
        .select_related("restaurant", "table")
        .prefetch_related("items__menu_item"),
        id=order_id,
    )

    owner_transitions = {
        "NEW": [("ACCEPTED", "Accept Order")],
        "ACCEPTED": [],
        "PREPARING": [],
        "READY": [("SERVED", "Mark Served")],
        "SERVED": [("COMPLETED", "Complete Order")] if order.payment_status == "PAID" else [],
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

        return redirect("order_detail", order_id=order.id)

    _order_age_metadata(order, timezone.now())
    timeline_choices = [c for c in Order.STATUS_CHOICES if c[0] != "CANCELLED"]
    if order.status == "CANCELLED":
        status_timeline = [
            {"label": label, "state": "done"}
            for _, label in timeline_choices
        ] + [{"label": "Cancelled", "state": "current"}]
    else:
        status_values = [value for value, _ in timeline_choices]
        current_step = status_values.index(order.status)
        status_timeline = [
            {
                "label": label,
                "state": "current" if index == current_step else (
                    "done" if index < current_step else "upcoming"
                ),
            }
            for index, (_, label) in enumerate(timeline_choices)
        ]

    show_collect_payment = (order.status == "SERVED" and order.payment_status != "PAID")

    is_stale_unfinished = (
        order.status in LIVE_STATUSES
        and order.created_at < get_live_order_cutoff(timezone.now())
    )

    return render(
        request,
        "dashboard/order_detail.html",
        {
            "order": order,
            "status_choices": owner_transitions.get(order.status, []),
            "status_timeline": status_timeline,
            "show_collect_payment": show_collect_payment,
            "is_stale_unfinished": is_stale_unfinished,
        },
    )


@role_required("admin", "owner", "manager")
def archive_stale_order_view(request, order_id):
    if request.method != "POST":
        return redirect("stale_orders_list")

    order = get_object_or_404(
        orders_for_user(request.user),
        id=order_id,
    )
    archive_stale_order(order, request.user)
    messages.success(
        request,
        f"Order #{order.id} was safely closed & archived. Table and inventory holds were released.",
    )
    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url:
        return redirect(next_url)
    return redirect("stale_orders_list")


@role_required("admin", "owner", "manager")
def archive_all_stale_orders_view(request):
    if request.method != "POST":
        return redirect("stale_orders_list")

    now = timezone.now()
    stale_cutoff = get_live_order_cutoff(now)
    stale_orders = list(
        orders_for_user(request.user).filter(
            status__in=LIVE_STATUSES,
            created_at__lt=stale_cutoff,
        )
    )

    count = 0
    for order in stale_orders:
        archive_stale_order(order, request.user)
        count += 1

    messages.success(
        request,
        f"Successfully closed & archived {count} stale unfinished order(s). All affected tables were released.",
    )
    return redirect("stale_orders_list")


@login_required(login_url='/auth/login/')
def category_list(request):
    categories = Category.objects.select_related("restaurant").order_by("name")
    return render(
        request,
        "dashboard/categories.html",
        {"categories": categories},
    )


@login_required(login_url='/auth/login/')
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


@login_required(login_url='/auth/login/')
def category_edit(request, category_id):
    category = get_object_or_404(Category, id=category_id)

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


@login_required(login_url='/auth/login/')
def category_delete(request, category_id):
    category = get_object_or_404(Category, id=category_id)

    if request.method == "POST":
        category.delete()

    return redirect("category_list")
