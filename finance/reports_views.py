from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.core.exceptions import PermissionDenied
from restaurant.branch_services import get_active_branch

from .date_utils import parse_date_range
from .reports_services import (
    get_item_sales_report_data,
    get_profit_and_loss_data,
    get_purchase_report_data,
    get_sales_report_data,
    get_stock_report_data,
)


def _report_scope(request):
    if not request.user.is_superuser and (request.user.role == "waiter" or not getattr(request.user, "can_view_reports", lambda: True)()):
        raise PermissionDenied("Waiters do not have permission to view reports.")
    # Do not let the legacy restaurant fallback expose another tenant's reports.
    restaurant = getattr(request.user, "restaurant", None)
    if restaurant is None:
        profile = getattr(request.user, "employee_profile", None)
        restaurant = getattr(profile, "restaurant", None)
    if restaurant is None:
        raise PermissionDenied("A restaurant is required for reporting.")
    can_consolidate = request.user.role == "owner"
    consolidated = request.GET.get("scope") == "all"
    if consolidated and not can_consolidate:
        raise PermissionDenied("Only Owners can view All Branches reports.")
    branch = get_active_branch(request, restaurant)
    if not consolidated and (branch is None or branch.restaurant_id != restaurant.pk):
        raise PermissionDenied("An active branch is required for reporting.")
    return restaurant, None if consolidated else branch, {
        "report_branch": None if consolidated else branch,
        "report_scope": "all" if consolidated else "branch",
        "report_consolidated": consolidated,
        "can_consolidate_reports": can_consolidate,
    }


@login_required(login_url="/auth/login/")
def sales_report(request):
    restaurant, branch, scope_context = _report_scope(request)
    date_filter = parse_date_range(request, default_preset="today")

    data = get_sales_report_data(
        restaurant=restaurant,
        branch=branch,
        start_datetime=date_filter["start_datetime"],
        end_datetime=date_filter["end_datetime"],
    )

    return render(
        request,
        "reports/report_sales.html",
        {
            "restaurant": restaurant,
            "date_filter": date_filter,
            **data,
            **scope_context,
        },
    )


@login_required(login_url="/auth/login/")
def item_sales_report(request):
    restaurant, branch, scope_context = _report_scope(request)
    date_filter = parse_date_range(request, default_preset="this_month")

    data = get_item_sales_report_data(
        restaurant=restaurant,
        branch=branch,
        start_datetime=date_filter["start_datetime"],
        end_datetime=date_filter["end_datetime"],
    )

    return render(
        request,
        "reports/report_item_sales.html",
        {
            "restaurant": restaurant,
            "date_filter": date_filter,
            **data,
            **scope_context,
        },
    )


@login_required(login_url="/auth/login/")
def stock_report(request):
    restaurant, branch, scope_context = _report_scope(request)
    date_filter = parse_date_range(request, default_preset="this_month")

    data = get_stock_report_data(
        restaurant=restaurant,
        branch=branch,
        start_datetime=date_filter["start_datetime"],
        end_datetime=date_filter["end_datetime"],
    )

    return render(
        request,
        "reports/report_stock.html",
        {
            "restaurant": restaurant,
            "date_filter": date_filter,
            **data,
            **scope_context,
        },
    )


@login_required(login_url="/auth/login/")
def purchase_report(request):
    restaurant, branch, scope_context = _report_scope(request)
    date_filter = parse_date_range(request, default_preset="this_month")

    data = get_purchase_report_data(
        restaurant=restaurant,
        branch=branch,
        start_date=date_filter["start_date"],
        end_date=date_filter["end_date"],
    )

    return render(
        request,
        "reports/report_purchases.html",
        {
            "restaurant": restaurant,
            "date_filter": date_filter,
            **data,
            **scope_context,
        },
    )


@login_required(login_url="/auth/login/")
def profit_loss_report(request):
    restaurant, branch, scope_context = _report_scope(request)
    date_filter = parse_date_range(request, default_preset="this_month")

    data = get_profit_and_loss_data(
        restaurant=restaurant,
        branch=branch,
        start_date=date_filter["start_date"],
        end_date=date_filter["end_date"],
        start_datetime=date_filter["start_datetime"],
        end_datetime=date_filter["end_datetime"],
    )

    return render(
        request,
        "reports/report_profit_loss.html",
        {
            "restaurant": restaurant,
            "date_filter": date_filter,
            **data,
            **scope_context,
        },
    )
