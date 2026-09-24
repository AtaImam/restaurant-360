from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST

from menu.models import Category, MenuItem
from orders.models import Coupon
from restaurant.models import Restaurant
from users.decorators import manager_required


def _get_active_restaurant(request):
    if hasattr(request, "user") and request.user.is_authenticated:
        if getattr(request.user, "restaurant", None):
            return request.user.restaurant
    return Restaurant.objects.first()


def _parse_dt_input(dt_str):
    if not dt_str or not dt_str.strip():
        return None
    dt = parse_datetime(dt_str.strip())
    if dt and timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


@manager_required
def coupon_list(request):
    restaurant = _get_active_restaurant(request)
    coupons = (
        Coupon.objects.filter(restaurant=restaurant)
        .prefetch_related("applicable_categories", "applicable_items")
        .order_by("-id")
    )

    q = request.GET.get("q", "").strip()
    if q:
        from django.db.models import Q
        coupons = coupons.filter(Q(name__icontains=q) | Q(code__icontains=q))

    status_filter = request.GET.get("status", "").strip().lower()
    now = timezone.now()
    if status_filter == "active":
        coupons = coupons.filter(is_active=True)
    elif status_filter == "inactive":
        coupons = coupons.filter(is_active=False)
    elif status_filter == "automatic":
        coupons = coupons.filter(is_automatic=True)
    elif status_filter == "expired":
        coupons = coupons.filter(end_datetime__lt=now)

    return render(
        request,
        "menu/coupon_list.html",
        {
            "coupons": coupons,
            "restaurant": restaurant,
            "q": q,
            "status_filter": status_filter,
            "total_coupons": coupons.count(),
            "current_url": "coupon_list",
        },
    )


@manager_required
def coupon_create(request):
    restaurant = _get_active_restaurant(request)
    categories = Category.objects.filter(restaurant=restaurant).order_by("name")
    menu_items = (
        MenuItem.objects.filter(category__restaurant=restaurant)
        .select_related("category")
        .order_by("category__name", "name")
    )

    error_message = None

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        code = request.POST.get("code", "").strip().upper()
        is_automatic = request.POST.get("is_automatic") in ("on", "1", "true", "True")
        discount_type = request.POST.get("discount_type", Coupon.DISCOUNT_TYPE_PERCENTAGE).strip()
        discount_val_str = request.POST.get("discount_value", "").strip()
        max_disc_str = request.POST.get("max_discount_amount", "").strip()
        min_order_str = request.POST.get("min_order_amount", "0").strip()
        start_dt_str = request.POST.get("start_datetime", "").strip()
        end_dt_str = request.POST.get("end_datetime", "").strip()
        usage_limit_str = request.POST.get("usage_limit", "").strip()
        is_active = request.POST.get("is_active") in ("on", "1", "true", "True")
        applicability = request.POST.get("applicability", Coupon.APPLICABILITY_WHOLE_ORDER).strip()
        category_ids = request.POST.getlist("categories")
        menu_item_ids = request.POST.getlist("menu_items")

        try:
            if not name:
                raise ValidationError("Offer / Coupon name is required.")

            try:
                discount_value = Decimal(discount_val_str)
                if discount_value <= Decimal("0.00"):
                    raise ValueError
            except (InvalidOperation, ValueError):
                raise ValidationError("Discount value must be a positive number.")

            if discount_type == Coupon.DISCOUNT_TYPE_PERCENTAGE and discount_value > Decimal("100.00"):
                raise ValidationError("Percentage discount cannot exceed 100%.")

            max_discount_amount = None
            if max_disc_str:
                try:
                    max_discount_amount = Decimal(max_disc_str)
                    if max_discount_amount <= Decimal("0.00"):
                        raise ValueError
                except (InvalidOperation, ValueError):
                    raise ValidationError("Maximum discount cap must be a positive number.")

            try:
                min_order_amount = Decimal(min_order_str or "0.00")
                if min_order_amount < Decimal("0.00"):
                    min_order_amount = Decimal("0.00")
            except (InvalidOperation, ValueError):
                min_order_amount = Decimal("0.00")

            usage_limit = None
            if usage_limit_str:
                try:
                    usage_limit = int(usage_limit_str)
                    if usage_limit < 1:
                        raise ValueError
                except ValueError:
                    raise ValidationError("Usage limit must be at least 1.")

            start_datetime = _parse_dt_input(start_dt_str)
            end_datetime = _parse_dt_input(end_dt_str)
            if start_datetime and end_datetime and start_datetime >= end_datetime:
                raise ValidationError("Expiry date/time must be after start date/time.")

            # Uniqueness check for code in same restaurant
            if code:
                if Coupon.objects.filter(restaurant=restaurant, code__iexact=code).exists():
                    raise ValidationError(f"Coupon code '{code}' already exists in this restaurant.")

            coupon = Coupon.objects.create(
                restaurant=restaurant,
                name=name,
                code=code,
                is_automatic=is_automatic,
                discount_type=discount_type,
                discount_value=discount_value,
                max_discount_amount=max_discount_amount,
                min_order_amount=min_order_amount,
                start_datetime=start_datetime,
                end_datetime=end_datetime,
                usage_limit=usage_limit,
                is_active=is_active,
                applicability=applicability,
            )

            if applicability == Coupon.APPLICABILITY_CATEGORY:
                coupon.applicable_categories.set(category_ids)
            elif applicability == Coupon.APPLICABILITY_MENU_ITEM:
                coupon.applicable_items.set(menu_item_ids)

            messages.success(request, f"Coupon '{coupon.name}' created successfully.")
            return redirect("coupon_list")

        except ValidationError as err:
            error_message = getattr(err, "message", None) or " ".join(err.messages)

    return render(
        request,
        "menu/coupon_form.html",
        {
            "restaurant": restaurant,
            "categories": categories,
            "menu_items": menu_items,
            "error_message": error_message,
            "is_create": True,
            "current_url": "coupon_create",
        },
    )


@manager_required
def coupon_edit(request, coupon_id):
    restaurant = _get_active_restaurant(request)
    coupon = get_object_or_404(Coupon, pk=coupon_id, restaurant=restaurant)
    categories = Category.objects.filter(restaurant=restaurant).order_by("name")
    menu_items = (
        MenuItem.objects.filter(category__restaurant=restaurant)
        .select_related("category")
        .order_by("category__name", "name")
    )

    error_message = None

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        code = request.POST.get("code", "").strip().upper()
        is_automatic = request.POST.get("is_automatic") in ("on", "1", "true", "True")
        discount_type = request.POST.get("discount_type", Coupon.DISCOUNT_TYPE_PERCENTAGE).strip()
        discount_val_str = request.POST.get("discount_value", "").strip()
        max_disc_str = request.POST.get("max_discount_amount", "").strip()
        min_order_str = request.POST.get("min_order_amount", "0").strip()
        start_dt_str = request.POST.get("start_datetime", "").strip()
        end_dt_str = request.POST.get("end_datetime", "").strip()
        usage_limit_str = request.POST.get("usage_limit", "").strip()
        is_active = request.POST.get("is_active") in ("on", "1", "true", "True")
        applicability = request.POST.get("applicability", Coupon.APPLICABILITY_WHOLE_ORDER).strip()
        category_ids = request.POST.getlist("categories")
        menu_item_ids = request.POST.getlist("menu_items")

        try:
            if not name:
                raise ValidationError("Offer / Coupon name is required.")

            try:
                discount_value = Decimal(discount_val_str)
                if discount_value <= Decimal("0.00"):
                    raise ValueError
            except (InvalidOperation, ValueError):
                raise ValidationError("Discount value must be a positive number.")

            if discount_type == Coupon.DISCOUNT_TYPE_PERCENTAGE and discount_value > Decimal("100.00"):
                raise ValidationError("Percentage discount cannot exceed 100%.")

            max_discount_amount = None
            if max_disc_str:
                try:
                    max_discount_amount = Decimal(max_disc_str)
                    if max_discount_amount <= Decimal("0.00"):
                        raise ValueError
                except (InvalidOperation, ValueError):
                    raise ValidationError("Maximum discount cap must be a positive number.")

            try:
                min_order_amount = Decimal(min_order_str or "0.00")
                if min_order_amount < Decimal("0.00"):
                    min_order_amount = Decimal("0.00")
            except (InvalidOperation, ValueError):
                min_order_amount = Decimal("0.00")

            usage_limit = None
            if usage_limit_str:
                try:
                    usage_limit = int(usage_limit_str)
                    if usage_limit < 1:
                        raise ValueError
                except ValueError:
                    raise ValidationError("Usage limit must be at least 1.")

            start_datetime = _parse_dt_input(start_dt_str)
            end_datetime = _parse_dt_input(end_dt_str)
            if start_datetime and end_datetime and start_datetime >= end_datetime:
                raise ValidationError("Expiry date/time must be after start date/time.")

            # Uniqueness check for code excluding current coupon
            if code:
                if (
                    Coupon.objects.filter(restaurant=restaurant, code__iexact=code)
                    .exclude(pk=coupon.pk)
                    .exists()
                ):
                    raise ValidationError(f"Coupon code '{code}' already exists in this restaurant.")

            coupon.name = name
            coupon.code = code
            coupon.is_automatic = is_automatic
            coupon.discount_type = discount_type
            coupon.discount_value = discount_value
            coupon.max_discount_amount = max_discount_amount
            coupon.min_order_amount = min_order_amount
            coupon.start_datetime = start_datetime
            coupon.end_datetime = end_datetime
            coupon.usage_limit = usage_limit
            coupon.is_active = is_active
            coupon.applicability = applicability
            coupon.save()

            if applicability == Coupon.APPLICABILITY_CATEGORY:
                coupon.applicable_categories.set(category_ids)
                coupon.applicable_items.clear()
            elif applicability == Coupon.APPLICABILITY_MENU_ITEM:
                coupon.applicable_items.set(menu_item_ids)
                coupon.applicable_categories.clear()
            else:
                coupon.applicable_categories.clear()
                coupon.applicable_items.clear()

            messages.success(request, f"Coupon '{coupon.name}' updated successfully.")
            return redirect("coupon_list")

        except ValidationError as err:
            error_message = getattr(err, "message", None) or " ".join(err.messages)

    selected_category_ids = set(coupon.applicable_categories.values_list("id", flat=True))
    selected_item_ids = set(coupon.applicable_items.values_list("id", flat=True))

    return render(
        request,
        "menu/coupon_form.html",
        {
            "restaurant": restaurant,
            "coupon": coupon,
            "categories": categories,
            "menu_items": menu_items,
            "selected_category_ids": selected_category_ids,
            "selected_item_ids": selected_item_ids,
            "error_message": error_message,
            "is_create": False,
            "current_url": "coupon_edit",
        },
    )


@require_POST
@manager_required
def coupon_toggle(request, coupon_id):
    restaurant = _get_active_restaurant(request)
    coupon = get_object_or_404(Coupon, pk=coupon_id, restaurant=restaurant)
    coupon.is_active = not coupon.is_active
    coupon.save(update_fields=["is_active", "updated_at"])
    status_label = "activated" if coupon.is_active else "deactivated"
    messages.success(request, f"Coupon '{coupon.name}' {status_label}.")
    return redirect("coupon_list")


@require_POST
@manager_required
def coupon_delete(request, coupon_id):
    restaurant = _get_active_restaurant(request)
    coupon = get_object_or_404(Coupon, pk=coupon_id, restaurant=restaurant)
    name = coupon.name
    coupon.delete()
    messages.success(request, f"Coupon '{name}' was deleted.")
    return redirect("coupon_list")
