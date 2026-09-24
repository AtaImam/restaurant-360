from django import template
from finance.money_utils import format_money, normalize_zero_money

register = template.Library()


@register.filter(name="money")
def money_filter(value):
    """Format value as ৳X.XX, normalizing negative zero to ৳0.00."""
    return format_money(value, currency="৳")


@register.filter(name="bdt")
def bdt_filter(value):
    """Alias for money filter."""
    return format_money(value, currency="৳")


@register.filter(name="normalize_zero")
def normalize_zero_filter(value):
    """Normalize monetary value, converting negative zero to Decimal('0.00')."""
    return normalize_zero_money(value)
