"""
Coupons & Offers Pricing and Discount Engine.

Handles coupon validation, percentage & fixed discount calculations,
automatic offer selection, whole-order/category/item scoping,
proportional OrderItem discount allocation, and atomic usage limit enforcement.
"""

from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from orders.models import Coupon


def money(value):
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(Decimal("0.01"))


def validate_and_calculate_coupon(coupon, items_data, now=None):
    """
    Validate coupon against current time, active flag, usage limit,
    cart items applicability, and minimum order requirements.

    Returns dict with:
      - coupon
      - eligible_items
      - eligible_subtotal
      - discount_amount
      - item_allocations
    """
    if now is None:
        now = timezone.now()

    if not coupon.is_active:
        raise ValidationError("This coupon/offer is not active.")

    if coupon.start_datetime and now < coupon.start_datetime:
        raise ValidationError("This coupon is not valid yet.")

    if coupon.end_datetime and now > coupon.end_datetime:
        raise ValidationError("This coupon has expired.")

    if coupon.usage_limit is not None and coupon.times_used >= coupon.usage_limit:
        raise ValidationError("This coupon has reached its maximum usage limit.")

    total_subtotal = sum(it["subtotal"] for it in items_data)

    # Determine eligible items based on applicability
    if coupon.applicability == Coupon.APPLICABILITY_WHOLE_ORDER:
        eligible_items = list(items_data)
    elif coupon.applicability == Coupon.APPLICABILITY_CATEGORY:
        applicable_cat_ids = set(coupon.applicable_categories.values_list("id", flat=True))
        eligible_items = [
            it for it in items_data
            if getattr(it["menu_item"], "category_id", None) in applicable_cat_ids
        ]
    elif coupon.applicability == Coupon.APPLICABILITY_MENU_ITEM:
        applicable_item_ids = set(coupon.applicable_items.values_list("id", flat=True))
        eligible_items = [
            it for it in items_data
            if it["menu_item"].id in applicable_item_ids
        ]
    else:
        eligible_items = list(items_data)

    eligible_subtotal = sum(it["subtotal"] for it in eligible_items)

    if not eligible_items or eligible_subtotal <= Decimal("0.00"):
        raise ValidationError("This coupon does not apply to any items in your order.")

    # Check minimum order amount
    eval_amount = total_subtotal if coupon.applicability == Coupon.APPLICABILITY_WHOLE_ORDER else eligible_subtotal
    if coupon.min_order_amount and eval_amount < coupon.min_order_amount:
        raise ValidationError(
            f"Minimum order amount of ৳{coupon.min_order_amount} required to use this coupon."
        )

    # Calculate discount
    if coupon.discount_type == Coupon.DISCOUNT_TYPE_PERCENTAGE:
        raw_discount = (eligible_subtotal * coupon.discount_value) / Decimal("100")
        if coupon.max_discount_amount is not None and raw_discount > coupon.max_discount_amount:
            discount_amount = coupon.max_discount_amount
        else:
            discount_amount = raw_discount
    elif coupon.discount_type == Coupon.DISCOUNT_TYPE_FIXED:
        discount_amount = min(coupon.discount_value, eligible_subtotal)
    else:
        discount_amount = Decimal("0.00")

    # Clamping & rounding
    discount_amount = min(discount_amount, total_subtotal)
    discount_amount = money(discount_amount)
    if discount_amount < Decimal("0.00"):
        discount_amount = Decimal("0.00")

    # Allocate discount across eligible items
    if discount_amount > Decimal("0.00") and eligible_subtotal > Decimal("0.00"):
        allocated_sum = Decimal("0.00")
        eligible_count = len(eligible_items)
        for idx, item in enumerate(eligible_items):
            if idx == eligible_count - 1:
                it_disc = discount_amount - allocated_sum
            else:
                it_disc = money((item["subtotal"] / eligible_subtotal) * discount_amount)
                allocated_sum += it_disc
            item["discount_amount"] = it_disc

        for item in items_data:
            if item not in eligible_items:
                item["discount_amount"] = Decimal("0.00")
    else:
        for item in items_data:
            item["discount_amount"] = Decimal("0.00")

    return {
        "coupon": coupon,
        "eligible_items": eligible_items,
        "eligible_subtotal": eligible_subtotal,
        "discount_amount": discount_amount,
    }


def find_best_automatic_offer(restaurant, items_data, now=None):
    """
    Evaluate all active automatic offers for the restaurant and cart items.
    Returns the calculation dict of the offer that produces the highest discount,
    or None if no automatic offer qualifies.
    """
    if now is None:
        now = timezone.now()

    auto_offers = (
        Coupon.objects.filter(
            restaurant=restaurant,
            is_active=True,
            is_automatic=True,
        )
        .prefetch_related("applicable_categories", "applicable_items")
    )

    best_result = None
    best_discount = Decimal("0.00")

    for offer in auto_offers:
        test_items = [dict(it) for it in items_data]
        try:
            res = validate_and_calculate_coupon(offer, test_items, now=now)
            if res["discount_amount"] > best_discount:
                best_discount = res["discount_amount"]
                best_result = {
                    "coupon": offer,
                    "discount_amount": best_discount,
                    "test_items": test_items,
                }
        except ValidationError:
            continue

    if best_result and best_discount > Decimal("0.00"):
        for actual, tested in zip(items_data, best_result["test_items"]):
            actual["discount_amount"] = tested.get("discount_amount", Decimal("0.00"))
        return {
            "coupon": best_result["coupon"],
            "discount_amount": best_discount,
        }

    return None


def calculate_order_pricing(
    restaurant,
    items_data,
    coupon_code=None,
    manual_discount=Decimal("0.00"),
    service_percent=None,
    vat_percent=None,
    now=None,
    branch=None,
    settings_values=None,
):
    """
    Single unified pricing engine for both QR and POS checkout.
    Calculates subtotal, coupon or manual discount, service charge, VAT, and grand total.
    Ensures coupon OR manual discount (never stacked), and allocates item discounts.
    """
    if now is None:
        now = timezone.now()

    from business_settings.services import resolve_settings, order_snapshot
    settings_values = settings_values if settings_values is not None else resolve_settings(restaurant, branch)
    if service_percent is None:
        service_percent = settings_values['service_percent']
    if vat_percent is None:
        vat_percent = settings_values['vat_percent']

    manual_discount = money(manual_discount)
    if manual_discount < Decimal("0.00"):
        manual_discount = Decimal("0.00")

    service_percent = Decimal(str(service_percent or 0))
    vat_percent = Decimal(str(vat_percent or 0))
    if service_percent < Decimal("0.00"):
        service_percent = Decimal("0.00")
    if service_percent > Decimal("100.00"):
        service_percent = Decimal("100.00")
    if vat_percent < Decimal("0.00"):
        vat_percent = Decimal("0.00")
    if vat_percent > Decimal("100.00"):
        vat_percent = Decimal("100.00")

    code_str = str(coupon_code or "").strip().upper()

    applied_coupon = None
    discount_amount = Decimal("0.00")

    if code_str:
        if manual_discount > Decimal("0.00"):
            raise ValidationError("Cannot combine a coupon code with a manual discount.")

        coupon = Coupon.objects.filter(
            restaurant=restaurant,
            code__iexact=code_str,
        ).first()

        if not coupon:
            raise ValidationError(f"Invalid coupon code '{code_str}'.")

        res = validate_and_calculate_coupon(coupon, items_data, now=now)
        applied_coupon = res["coupon"]
        discount_amount = res["discount_amount"]

    elif manual_discount > Decimal("0.00"):
        applied_coupon = None
        subtotal_calc = sum(it["subtotal"] for it in items_data)
        discount_amount = min(manual_discount, subtotal_calc)
        discount_amount = money(discount_amount)

        if discount_amount > Decimal("0.00") and subtotal_calc > Decimal("0.00"):
            allocated_sum = Decimal("0.00")
            item_count = len(items_data)
            for idx, it in enumerate(items_data):
                if idx == item_count - 1:
                    it_disc = discount_amount - allocated_sum
                else:
                    it_disc = money((it["subtotal"] / subtotal_calc) * discount_amount)
                    allocated_sum += it_disc
                it["discount_amount"] = it_disc
        else:
            for it in items_data:
                it["discount_amount"] = Decimal("0.00")

    else:
        auto_res = find_best_automatic_offer(restaurant, items_data, now=now)
        if auto_res:
            applied_coupon = auto_res["coupon"]
            discount_amount = auto_res["discount_amount"]
        else:
            applied_coupon = None
            discount_amount = Decimal("0.00")
            for it in items_data:
                it["discount_amount"] = Decimal("0.00")

    subtotal = money(sum(it["subtotal"] for it in items_data))
    discount_amount = money(min(discount_amount, subtotal))
    after_discount = money(max(Decimal("0.00"), subtotal - discount_amount))
    service_charge = money((after_discount * service_percent) / Decimal("100"))
    taxable_amount = after_discount + service_charge
    vat_amount = money((taxable_amount * vat_percent) / Decimal("100"))
    total_amount = money(taxable_amount + vat_amount)

    coupon_code_snapshot = applied_coupon.code if applied_coupon else ""
    if applied_coupon:
        coupon_name_snapshot = applied_coupon.name
        discount_type_snapshot = applied_coupon.discount_type
        discount_rate_snapshot = applied_coupon.discount_value
    elif discount_amount > Decimal("0.00"):
        coupon_name_snapshot = "Manual Discount"
        discount_type_snapshot = Coupon.DISCOUNT_TYPE_FIXED
        discount_rate_snapshot = discount_amount
    else:
        coupon_name_snapshot = ""
        discount_type_snapshot = ""
        discount_rate_snapshot = None

    return {
        "settings_snapshot": order_snapshot(restaurant, branch, {**settings_values, "service_percent": str(service_percent), "vat_percent": str(vat_percent)}),
        "subtotal": subtotal,
        "discount_amount": discount_amount,
        "after_discount": after_discount,
        "service_charge": service_charge,
        "vat_amount": vat_amount,
        "total_amount": total_amount,
        "applied_coupon": applied_coupon,
        "coupon_code_snapshot": coupon_code_snapshot,
        "coupon_name_snapshot": coupon_name_snapshot,
        "discount_type_snapshot": discount_type_snapshot,
        "discount_rate_snapshot": discount_rate_snapshot,
        "items_data": items_data,
    }


def apply_coupon_usage_atomic(coupon_id):
    """
    Lock coupon row, enforce usage limit, and increment times_used atomically.
    Must be called inside an active transaction.atomic() block.
    """
    if not coupon_id:
        return None

    locked = Coupon.objects.select_for_update().get(id=coupon_id)
    if not locked.is_active:
        raise ValidationError("This coupon is no longer active.")

    now = timezone.now()
    if locked.start_datetime and now < locked.start_datetime:
        raise ValidationError("This coupon is not valid yet.")

    if locked.end_datetime and now > locked.end_datetime:
        raise ValidationError("This coupon has expired.")

    if locked.usage_limit is not None and locked.times_used >= locked.usage_limit:
        raise ValidationError("This coupon has reached its maximum usage limit.")

    Coupon.objects.filter(id=locked.id).update(times_used=F("times_used") + 1)
    locked.refresh_from_db(fields=["times_used"])
    return locked
