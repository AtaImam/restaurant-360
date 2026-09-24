from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Sum
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST, require_safe

from orders.models import Order
from orders.services import record_order_refund
from users.decorators import role_required
from .date_utils import get_restaurant_context, parse_date_range
from .forms import ExpenseCategoryForm, ExpenseForm
from .models import Expense, ExpenseCategory


def check_finance_access(request):
    if not request.user.is_authenticated:
        raise PermissionDenied("Please log in.")
    if not request.user.is_superuser and (request.user.role == "waiter" or request.user.role not in ["admin", "owner", "manager"]):
        raise PermissionDenied("Waiters do not have permission to access financial management.")


# ---------------------------------------------------------------------------
# Expense Category CRUD
# ---------------------------------------------------------------------------

@login_required(login_url="/auth/login/")
def category_list(request):
    check_finance_access(request)
    restaurant = get_restaurant_context(request)
    categories = ExpenseCategory.objects.filter(restaurant=restaurant)
    form = ExpenseCategoryForm()

    return render(
        request,
        "finance/expense_category_list.html",
        {
            "categories": categories,
            "form": form,
            "restaurant": restaurant,
        },
    )


@login_required(login_url="/auth/login/")
def category_create(request):
    check_finance_access(request)
    restaurant = get_restaurant_context(request)
    if request.method == "POST":
        form = ExpenseCategoryForm(request.POST)
        if form.is_valid():
            cat = form.save(commit=False)
            cat.restaurant = restaurant
            try:
                cat.save()
                messages.success(request, f"Category '{cat.name}' created successfully.")
                return redirect("finance:category_list")
            except Exception as e:
                messages.error(request, f"Could not create category: {e}")
        else:
            messages.error(request, "Please correct the form errors.")
    else:
        form = ExpenseCategoryForm()

    return render(
        request,
        "finance/expense_category_form.html",
        {
            "form": form,
            "restaurant": restaurant,
            "is_edit": False,
        },
    )


@login_required(login_url="/auth/login/")
def category_edit(request, category_id):
    check_finance_access(request)
    restaurant = get_restaurant_context(request)
    cat = get_object_or_404(ExpenseCategory, pk=category_id, restaurant=restaurant)

    if request.method == "POST":
        form = ExpenseCategoryForm(request.POST, instance=cat)
        if form.is_valid():
            form.save()
            messages.success(request, f"Category '{cat.name}' updated successfully.")
            return redirect("finance:category_list")
        else:
            messages.error(request, "Please correct the form errors.")
    else:
        form = ExpenseCategoryForm(instance=cat)

    return render(
        request,
        "finance/expense_category_form.html",
        {
            "form": form,
            "category": cat,
            "restaurant": restaurant,
            "is_edit": True,
        },
    )


@login_required(login_url="/auth/login/")
@require_POST
def category_delete(request, category_id):
    check_finance_access(request)
    restaurant = get_restaurant_context(request)
    cat = get_object_or_404(ExpenseCategory, pk=category_id, restaurant=restaurant)
    if cat.expenses.exists():
        messages.error(request, f"Cannot delete '{cat.name}' because it contains logged expenses. Deactivate it instead.")
    else:
        cat.delete()
        messages.success(request, f"Category '{cat.name}' deleted successfully.")
    return redirect("finance:category_list")


# ---------------------------------------------------------------------------
# Expense CRUD
# ---------------------------------------------------------------------------

@login_required(login_url="/auth/login/")
def expense_list(request):
    check_finance_access(request)
    restaurant = get_restaurant_context(request)
    date_filter = parse_date_range(request, default_preset="this_month")

    expenses = Expense.objects.filter(
        restaurant=restaurant,
        expense_date__gte=date_filter["start_date"],
        expense_date__lte=date_filter["end_date"],
    ).select_related("category", "created_by")

    # Filter by category
    selected_category_id = request.GET.get("category", "").strip()
    if selected_category_id.isdigit():
        expenses = expenses.filter(category_id=int(selected_category_id))

    # Filter by payment method
    selected_method = request.GET.get("payment_method", "").strip()
    if selected_method:
        expenses = expenses.filter(payment_method=selected_method)

    # Search keyword
    search_q = request.GET.get("search", "").strip()
    if search_q:
        expenses = expenses.filter(title__icontains=search_q)

    # Summary calculation
    total_amount = expenses.aggregate(
        total=Coalesce(Sum("amount"), Decimal("0.00"))
    )["total"]

    categories = ExpenseCategory.objects.filter(restaurant=restaurant, is_active=True)

    paginator = Paginator(expenses, 25)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    return render(
        request,
        "finance/expense_list.html",
        {
            "restaurant": restaurant,
            "page_obj": page_obj,
            "total_amount": total_amount,
            "date_filter": date_filter,
            "categories": categories,
            "selected_category_id": selected_category_id,
            "selected_method": selected_method,
            "search_q": search_q,
            "payment_methods": Expense.PAYMENT_METHOD_CHOICES,
        },
    )


@login_required(login_url="/auth/login/")
def expense_create(request):
    check_finance_access(request)
    restaurant = get_restaurant_context(request)

    if request.method == "POST":
        form = ExpenseForm(request.POST, request.FILES, restaurant=restaurant)
        if form.is_valid():
            expense = form.save(commit=False)
            expense.restaurant = restaurant
            expense.created_by = request.user
            expense.save()
            messages.success(request, f"Expense '{expense.title}' of {expense.amount} recorded.")
            return redirect("finance:expense_list")
        else:
            messages.error(request, "Please correct the form errors.")
    else:
        form = ExpenseForm(restaurant=restaurant)

    return render(
        request,
        "finance/expense_form.html",
        {
            "form": form,
            "restaurant": restaurant,
            "is_edit": False,
        },
    )


@login_required(login_url="/auth/login/")
def expense_edit(request, expense_id):
    check_finance_access(request)
    restaurant = get_restaurant_context(request)
    expense = get_object_or_404(Expense, pk=expense_id, restaurant=restaurant)

    if request.method == "POST":
        form = ExpenseForm(request.POST, request.FILES, instance=expense, restaurant=restaurant)
        if form.is_valid():
            form.save()
            messages.success(request, f"Expense '{expense.title}' updated.")
            return redirect("finance:expense_list")
        else:
            messages.error(request, "Please correct the form errors.")
    else:
        form = ExpenseForm(instance=expense, restaurant=restaurant)

    return render(
        request,
        "finance/expense_form.html",
        {
            "form": form,
            "expense": expense,
            "restaurant": restaurant,
            "is_edit": True,
        },
    )


@login_required(login_url="/auth/login/")
@require_POST
def expense_delete(request, expense_id):
    check_finance_access(request)
    restaurant = get_restaurant_context(request)
    expense = get_object_or_404(Expense, pk=expense_id, restaurant=restaurant)
    expense.delete()
    messages.success(request, f"Expense '{expense.title}' deleted successfully.")
    return redirect("finance:expense_list")


# ---------------------------------------------------------------------------
# Refund Recording Endpoint
# ---------------------------------------------------------------------------

@login_required(login_url="/auth/login/")
@require_POST
def record_refund_view(request, order_id):
    check_finance_access(request)
    restaurant = get_restaurant_context(request)
    order = get_object_or_404(Order, pk=order_id, restaurant=restaurant)

    amount_str = request.POST.get("amount", "").strip()
    reason = request.POST.get("reason", "").strip()
    payment_method = request.POST.get("payment_method", "").strip()

    try:
        amount = Decimal(amount_str)
        txn = record_order_refund(
            order=order,
            amount=amount,
            reason=reason,
            payment_method=payment_method or None,
            recorded_by=request.user,
        )
        messages.success(request, f"Refund of {txn.amount} recorded for Order #{order.id}.")
    except (ValidationError, ValueError) as err:
        messages.error(request, str(err))

    return redirect("order_detail", order_id=order.id)
