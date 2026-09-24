from datetime import time
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import BranchSettings, RestaurantSettings
from .schema import DEFAULTS


def resolve_settings(restaurant, branch=None):
    """The only resolution order: defaults → restaurant → explicit branch values."""
    if branch is not None and (restaurant is None or branch.restaurant_id != restaurant.pk):
        raise ValidationError('Branch does not belong to this restaurant.')
    values = dict(DEFAULTS)
    if restaurant is not None:
        values.update(RestaurantSettings.objects.filter(restaurant=restaurant).values_list('values', flat=True).first() or {})
    if branch is not None:
        values.update(BranchSettings.objects.filter(branch=branch).values_list('values', flat=True).first() or {})
    return values


def payment_choices(values):
    from orders.models import Order
    return [(code, label) for code, label in Order.PAYMENT_METHOD_CHOICES if values['payment_' + code.lower()]]


def validate_new_order(values, order_type, payment_timing, payment_method='', now=None):
    if order_type not in {'DINE_IN', 'TAKEAWAY'} or not values['allow_' + order_type.lower()]:
        raise ValidationError('This order type is disabled for this branch.')
    if payment_timing not in {'PAY_NOW', 'PAY_LATER'} or not values['allow_' + payment_timing.lower()]:
        raise ValidationError('This payment timing is disabled for this branch.')
    if payment_timing == 'PAY_NOW' and payment_method not in dict(payment_choices(values)):
        raise ValidationError('This payment method is disabled for this branch.')
    if values['enforce_hours']:
        current = timezone.localtime(now or timezone.now(), ZoneInfo(values['timezone'])).time()
        opening, closing = time.fromisoformat(values['opening_time']), time.fromisoformat(values['closing_time'])
        is_open = opening <= current < closing if opening < closing else current >= opening or current < closing
        if not is_open:
            raise ValidationError('This branch is currently outside its operating hours.')


def order_snapshot(restaurant, branch=None, values=None, applied_rates=True):
    values = values if values is not None else resolve_settings(restaurant, branch)
    return {
        'version': 1,
        'currency': 'BDT',
        'vat_percent': str(values['vat_percent']) if applied_rates else None,
        'service_percent': str(values['service_percent']) if applied_rates else None,
        'tax_policy': 'exclusive_after_discount_and_service' if applied_rates else 'legacy_unknown',
        'timezone': values['timezone'],
        'receipt_name': values['receipt_name'] or restaurant.name,
        'branch_name': branch.name if branch else '',
        **{key: values[key] for key in ('receipt_logo_url', 'receipt_contact', 'receipt_heading', 'receipt_footer', 'receipt_show_branch', 'tax_registration')},
    }
