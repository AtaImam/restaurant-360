from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Count, F, Q, Sum
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from business_settings.services import resolve_settings
from orders.models import Order, OrderItem, PaymentTransaction
from orders.services import LIVE_STATUSES
from restaurant.branch_services import get_active_branch
from restaurant.models import Restaurant

DEFAULT_DASHBOARD_TIMEZONE = "Asia/Dhaka"


def get_dashboard_timezone(restaurant, branch=None):
    """Resolve the configured timezone from business settings, falling back to Asia/Dhaka."""
    tz_str = ""
    if restaurant is not None:
        try:
            settings_values = resolve_settings(restaurant, branch)
            tz_str = (settings_values.get("timezone") or "").strip()
        except Exception:
            pass

    if not tz_str:
        tz_str = DEFAULT_DASHBOARD_TIMEZONE

    try:
        return ZoneInfo(tz_str), tz_str
    except (ZoneInfoNotFoundError, Exception):
        return ZoneInfo(DEFAULT_DASHBOARD_TIMEZONE), DEFAULT_DASHBOARD_TIMEZONE


def _pct_change(current, previous):
    c = float(current)
    p = float(previous)
    if p > 0:
        return round(((c - p) / p) * 100, 1)
    elif p == 0 and c > 0:
        return 100.0
    return 0.0


def get_dashboard_analytics(request, period="7d", now=None):
    """Calculate comprehensive dashboard analytics with branch isolation and period filtering.

    All metrics are backed by real database data with zero mock/fake values.
    Uses the configured restaurant/business timezone from Settings (fallback: Asia/Dhaka).
    """
    if now is None:
        now = timezone.now()

    # 1. Resolve restaurant context
    user = request.user
    restaurant = getattr(user, "restaurant", None)
    if restaurant is None:
        profile = getattr(user, "employee_profile", None)
        restaurant = getattr(profile, "restaurant", None)
    if restaurant is None:
        restaurant = Restaurant.objects.order_by("pk").first()

    # 2. Resolve branch context & consolidated mode
    can_consolidate = (
        getattr(user, "role", None) in ("owner", "admin") or user.is_superuser
    )
    scope = request.GET.get("scope", "").strip().lower()
    is_consolidated = (scope == "all" and can_consolidate)

    branch = None if is_consolidated else get_active_branch(request, restaurant)

    # 3. Resolve timezone (configured restaurant/branch setting -> fallback Asia/Dhaka)
    tz, tz_name = get_dashboard_timezone(restaurant, branch)
    now_local = timezone.localtime(now, tz)
    today = now_local.date()
    yesterday = today - timedelta(days=1)

    today_start = timezone.make_aware(datetime.combine(today, time.min), tz)
    today_end = timezone.make_aware(datetime.combine(today, time.max), tz)
    yesterday_start = timezone.make_aware(datetime.combine(yesterday, time.min), tz)
    yesterday_end = timezone.make_aware(datetime.combine(yesterday, time.max), tz)

    # 3. Base QuerySets scoped by restaurant and branch
    base_orders = Order.objects.filter(restaurant=restaurant)
    base_txns = PaymentTransaction.objects.filter(restaurant=restaurant)

    if branch is not None:
        base_orders = base_orders.filter(branch=branch)
        base_txns = base_txns.filter(Q(branch=branch) | Q(order__branch=branch))

    # 4. KPI Metrics: Today vs Yesterday
    # Today's net settled revenue
    today_payments = base_txns.filter(
        transaction_at__gte=today_start,
        transaction_at__lte=today_end,
        transaction_type=PaymentTransaction.TYPE_PAYMENT,
    ).aggregate(total=Coalesce(Sum("amount"), Decimal("0.00")))["total"]

    today_refunds = base_txns.filter(
        transaction_at__gte=today_start,
        transaction_at__lte=today_end,
        transaction_type=PaymentTransaction.TYPE_REFUND,
    ).aggregate(total=Coalesce(Sum("amount"), Decimal("0.00")))["total"]

    today_revenue = max(Decimal("0.00"), today_payments - today_refunds)

    # Yesterday's net settled revenue
    yesterday_payments = base_txns.filter(
        transaction_at__gte=yesterday_start,
        transaction_at__lte=yesterday_end,
        transaction_type=PaymentTransaction.TYPE_PAYMENT,
    ).aggregate(total=Coalesce(Sum("amount"), Decimal("0.00")))["total"]

    yesterday_refunds = base_txns.filter(
        transaction_at__gte=yesterday_start,
        transaction_at__lte=yesterday_end,
        transaction_type=PaymentTransaction.TYPE_REFUND,
    ).aggregate(total=Coalesce(Sum("amount"), Decimal("0.00")))["total"]

    yesterday_revenue = max(Decimal("0.00"), yesterday_payments - yesterday_refunds)

    # Paid order counts for today and yesterday
    today_paid_ids = set(
        base_txns.filter(
            transaction_at__gte=today_start,
            transaction_at__lte=today_end,
            transaction_type=PaymentTransaction.TYPE_PAYMENT,
        )
        .exclude(order__status=Order.STATUS_CANCELLED)
        .values_list("order_id", flat=True)
    ) | set(
        base_orders.filter(
            payment_status="PAID",
            paid_at__gte=today_start,
            paid_at__lte=today_end,
        )
        .exclude(status=Order.STATUS_CANCELLED)
        .values_list("id", flat=True)
    )
    today_orders_count = len(today_paid_ids)

    yesterday_paid_ids = set(
        base_txns.filter(
            transaction_at__gte=yesterday_start,
            transaction_at__lte=yesterday_end,
            transaction_type=PaymentTransaction.TYPE_PAYMENT,
        )
        .exclude(order__status=Order.STATUS_CANCELLED)
        .values_list("order_id", flat=True)
    ) | set(
        base_orders.filter(
            payment_status="PAID",
            paid_at__gte=yesterday_start,
            paid_at__lte=yesterday_end,
        )
        .exclude(status=Order.STATUS_CANCELLED)
        .values_list("id", flat=True)
    )
    yesterday_orders_count = len(yesterday_paid_ids)

    # Active orders (live queue)
    active_orders_count = base_orders.filter(status__in=LIVE_STATUSES).count()

    # Average Order Value
    today_aov = (
        (today_revenue / Decimal(today_orders_count)).quantize(Decimal("0.01"))
        if today_orders_count > 0
        else Decimal("0.00")
    )
    yesterday_aov = (
        (yesterday_revenue / Decimal(yesterday_orders_count)).quantize(Decimal("0.01"))
        if yesterday_orders_count > 0
        else Decimal("0.00")
    )

    # Relative changes (% vs yesterday)
    revenue_change_pct = _pct_change(today_revenue, yesterday_revenue)
    orders_change_pct = _pct_change(today_orders_count, yesterday_orders_count)
    aov_change_pct = _pct_change(today_aov, yesterday_aov)

    # 5. Selected Period Range for Sales Trend & Item Analytics
    valid_periods = {"7d", "30d", "this_month"}
    period = period if period in valid_periods else "7d"

    if period == "30d":
        start_date = today - timedelta(days=29)
        period_label = "Last 30 Days"
    elif period == "this_month":
        start_date = today.replace(day=1)
        period_label = "This Month"
    else:
        period = "7d"
        start_date = today - timedelta(days=6)
        period_label = "Last 7 Days"

    period_start_dt = timezone.make_aware(datetime.combine(start_date, time.min), tz)
    period_end_dt = timezone.make_aware(datetime.combine(today, time.max), tz)

    # Generate daily dates list
    days_count = (today - start_date).days + 1
    date_list = [start_date + timedelta(days=i) for i in range(days_count)]

    # Aggregate daily payments, refunds, and order counts
    period_txns = base_txns.filter(
        transaction_at__gte=period_start_dt,
        transaction_at__lte=period_end_dt,
    )

    daily_payments = (
        period_txns.filter(transaction_type=PaymentTransaction.TYPE_PAYMENT)
        .annotate(day=TruncDate("transaction_at", tzinfo=tz))
        .values("day")
        .annotate(total=Sum("amount"))
    )
    daily_refunds = (
        period_txns.filter(transaction_type=PaymentTransaction.TYPE_REFUND)
        .annotate(day=TruncDate("transaction_at", tzinfo=tz))
        .values("day")
        .annotate(total=Sum("amount"))
    )
    daily_orders = (
        period_txns.filter(transaction_type=PaymentTransaction.TYPE_PAYMENT)
        .exclude(order__status=Order.STATUS_CANCELLED)
        .annotate(day=TruncDate("transaction_at", tzinfo=tz))
        .values("day")
        .annotate(count=Count("order_id", distinct=True))
    )

    # Also check direct paid orders for any orders without separate ledger transaction
    daily_direct_paid = (
        base_orders.filter(
            payment_status="PAID",
            paid_at__gte=period_start_dt,
            paid_at__lte=period_end_dt,
        )
        .exclude(status=Order.STATUS_CANCELLED)
        .annotate(day=TruncDate("paid_at", tzinfo=tz))
        .values("day")
        .annotate(total=Sum("total_amount"), count=Count("id"))
    )

    pay_map = {row["day"]: row["total"] for row in daily_payments}
    ref_map = {row["day"]: row["total"] for row in daily_refunds}
    ord_map = {row["day"]: row["count"] for row in daily_orders}
    dir_pay_map = {row["day"]: row["total"] for row in daily_direct_paid}
    dir_ord_map = {row["day"]: row["count"] for row in daily_direct_paid}

    trend_labels = []
    trend_dates = []
    trend_full_dates = []
    trend_revenues = []
    trend_orders = []

    for d in date_list:
        trend_labels.append(d.strftime("%b %d"))
        trend_dates.append(d.strftime("%Y-%m-%d"))
        trend_full_dates.append(d.strftime("%a, %b %d, %Y"))

        net_rev = pay_map.get(d, Decimal("0.00")) - ref_map.get(d, Decimal("0.00"))
        if net_rev <= Decimal("0.00") and d in dir_pay_map:
            net_rev = dir_pay_map[d]

        count = max(ord_map.get(d, 0), dir_ord_map.get(d, 0))

        trend_revenues.append(float(max(Decimal("0.00"), net_rev)))
        trend_orders.append(int(count))

    # 6. Order Status Distribution (Live queue + completed in period)
    status_counts = {
        "NEW": base_orders.filter(status="NEW").count(),
        "ACCEPTED": base_orders.filter(status="ACCEPTED").count(),
        "PREPARING": base_orders.filter(status="PREPARING").count(),
        "READY": base_orders.filter(status="READY").count(),
        "SERVED": base_orders.filter(status="SERVED").count(),
        "COMPLETED": base_orders.filter(
            status="COMPLETED",
            created_at__gte=period_start_dt,
        ).count(),
    }
    status_counts["total_active"] = (
        status_counts["NEW"]
        + status_counts["ACCEPTED"]
        + status_counts["PREPARING"]
        + status_counts["READY"]
    )

    # 7. Period Paid Orders & Items Analysis
    period_paid_order_ids = set(
        period_txns.filter(transaction_type=PaymentTransaction.TYPE_PAYMENT)
        .exclude(order__status=Order.STATUS_CANCELLED)
        .values_list("order_id", flat=True)
    ) | set(
        base_orders.filter(
            payment_status="PAID",
            paid_at__gte=period_start_dt,
            paid_at__lte=period_end_dt,
        )
        .exclude(status=Order.STATUS_CANCELLED)
        .values_list("id", flat=True)
    )

    # Top Selling Items
    top_items = []
    if period_paid_order_ids:
        items_agg = (
            OrderItem.objects.filter(
                order_id__in=period_paid_order_ids,
                order__restaurant=restaurant,
            )
            .values(
                "menu_item__id",
                "menu_item__name",
                "menu_item__category__name",
            )
            .annotate(
                qty_sold=Sum("quantity"),
                rev_generated=Sum(F("price") * F("quantity")),
            )
            .order_by("-qty_sold", "-rev_generated")[:5]
        )

        for rank, row in enumerate(items_agg, start=1):
            rev = row["rev_generated"] or Decimal("0.00")
            top_items.append({
                "rank": rank,
                "name": row["menu_item__name"],
                "category": row["menu_item__category__name"] or "Menu Item",
                "quantity": int(row["qty_sold"] or 0),
                "revenue": float(rev),
                "revenue_formatted": f"৳{rev:,.2f}",
            })

    # 8. Sales by Order Type (DINE_IN vs TAKEAWAY)
    period_orders_qs = base_orders.filter(id__in=period_paid_order_ids)
    dine_in_qs = period_orders_qs.filter(order_type="DINE_IN")
    takeaway_qs = period_orders_qs.filter(order_type="TAKEAWAY")

    dine_in_count = dine_in_qs.count()
    takeaway_count = takeaway_qs.count()
    total_type_orders = dine_in_count + takeaway_count

    dine_in_rev = dine_in_qs.aggregate(
        total=Coalesce(Sum("total_amount"), Decimal("0.00"))
    )["total"]
    takeaway_rev = takeaway_qs.aggregate(
        total=Coalesce(Sum("total_amount"), Decimal("0.00"))
    )["total"]
    total_type_rev = dine_in_rev + takeaway_rev

    dine_in_pct = (
        round((dine_in_count / total_type_orders) * 100, 1)
        if total_type_orders > 0
        else 0.0
    )
    takeaway_pct = (
        round((takeaway_count / total_type_orders) * 100, 1)
        if total_type_orders > 0
        else 0.0
    )

    # 9. Return structured data
    return {
        "kpis": {
            "today_revenue": float(today_revenue),
            "today_revenue_formatted": f"৳{today_revenue:,.2f}",
            "today_orders": today_orders_count,
            "active_orders": active_orders_count,
            "average_order_value": float(today_aov),
            "average_order_value_formatted": f"৳{today_aov:,.2f}",
            "yesterday_revenue": float(yesterday_revenue),
            "yesterday_revenue_formatted": f"৳{yesterday_revenue:,.2f}",
            "yesterday_orders": yesterday_orders_count,
            "yesterday_aov": float(yesterday_aov),
            "revenue_change_pct": revenue_change_pct,
            "orders_change_pct": orders_change_pct,
            "aov_change_pct": aov_change_pct,
        },
        "sales_trend": {
            "period": period,
            "period_label": period_label,
            "labels": trend_labels,
            "dates": trend_dates,
            "full_dates": trend_full_dates,
            "revenues": trend_revenues,
            "order_counts": trend_orders,
            "total_revenue": sum(trend_revenues),
            "total_revenue_formatted": f"৳{sum(trend_revenues):,.2f}",
            "total_orders": sum(trend_orders),
        },
        "order_status": status_counts,
        "top_items": top_items,
        "order_types": {
            "dine_in": {
                "count": dine_in_count,
                "revenue": float(dine_in_rev),
                "revenue_formatted": f"৳{dine_in_rev:,.2f}",
                "percentage": dine_in_pct,
            },
            "takeaway": {
                "count": takeaway_count,
                "revenue": float(takeaway_rev),
                "revenue_formatted": f"৳{takeaway_rev:,.2f}",
                "percentage": takeaway_pct,
            },
            "total_count": total_type_orders,
            "total_revenue": float(total_type_rev),
            "total_revenue_formatted": f"৳{total_type_rev:,.2f}",
        },
        "branch_context": {
            "is_consolidated": is_consolidated,
            "branch_id": branch.id if branch else None,
            "branch_name": (
                branch.name if branch else "All Branches — Consolidated"
            ),
            "can_consolidate": can_consolidate,
            "scope": "all" if is_consolidated else "branch",
        },
        "meta": {
            "timezone": tz_name,
            "today_date": today.strftime("%b %d, %Y"),
            "last_refreshed": now_local.strftime("%I:%M:%S %p"),
            "live_time": now_local.strftime("%I:%M:%S %p"),
        },
    }
