from copy import deepcopy
from decimal import Decimal
from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django import forms
from django.core.exceptions import ValidationError

SECTIONS = {
    'business': 'Business & Branches',
    'sales': 'Sales & Payments',
    'tax': 'Tax & Charges',
    'receipts': 'Receipts',
    'kitchen': 'Kitchen',
    'notifications': 'Notifications',
}
# JSON stores only validated, named settings; branch rows contain explicit overrides.
FIELDS = {
    'business': {
        'timezone': forms.CharField(initial='Asia/Dhaka', label='Business timezone', help_text='IANA name, for example Asia/Dhaka. Used for opening hours and receipts.'),
        'enforce_hours': forms.BooleanField(required=False, initial=False, label='Accept new orders only during operating hours'),
        'opening_time': forms.TimeField(initial='09:00', widget=forms.TimeInput(attrs={'type': 'time'})),
        'closing_time': forms.TimeField(initial='22:00', widget=forms.TimeInput(attrs={'type': 'time'})),
        'currency': forms.ChoiceField(choices=[('BDT', 'BDT — Bangladeshi Taka (৳)')], initial='BDT'),
    },
    'sales': {
        'allow_dine_in': forms.BooleanField(required=False, initial=True, label='Dine-in orders'),
        'allow_takeaway': forms.BooleanField(required=False, initial=True, label='Takeaway orders'),
        'allow_pay_now': forms.BooleanField(required=False, initial=True, label='Pay now'),
        'allow_pay_later': forms.BooleanField(required=False, initial=True, label='Pay later'),
        'payment_cash': forms.BooleanField(required=False, initial=True, label='Cash'),
        'payment_card': forms.BooleanField(required=False, initial=True, label='Card'),
        'payment_mobile_banking': forms.BooleanField(required=False, initial=True, label='Mobile banking'),
    },
    'tax': {
        'vat_percent': forms.DecimalField(initial='0.00', min_value=0, max_value=100, max_digits=5, decimal_places=2, label='VAT (%)'),
        'service_percent': forms.DecimalField(initial='0.00', min_value=0, max_value=100, max_digits=5, decimal_places=2, label='Service charge (%)'),
        'tax_registration': forms.CharField(required=False, max_length=100, label='Tax registration number'),
    },
    'receipts': {
        'receipt_name': forms.CharField(required=False, max_length=150, label='Receipt brand name', help_text='Blank uses the restaurant name.'),
        'receipt_logo_url': forms.URLField(required=False, max_length=500, label='Logo URL'),
        'receipt_contact': forms.CharField(required=False, max_length=1000, widget=forms.Textarea(attrs={'rows': 3}), label='Receipt contact / address'),
        'receipt_heading': forms.CharField(initial='Thank you! 🙏', required=False, max_length=150, label='Receipt closing heading'),
        'receipt_footer': forms.CharField(initial='We hope to see you again soon.', required=False, max_length=1000, widget=forms.Textarea(attrs={'rows': 3}), label='Receipt footer'),
        'receipt_show_branch': forms.BooleanField(initial=False, required=False, label='Show branch name'),
    },
    'kitchen': {
        'kitchen_sort': forms.ChoiceField(choices=[('newest', 'Newest first'), ('oldest', 'Oldest first')], initial='newest', label='Order display sequence'),
        'kitchen_show_dine_in': forms.BooleanField(required=False, initial=True, label='Show dine-in orders'),
        'kitchen_show_takeaway': forms.BooleanField(required=False, initial=True, label='Show takeaway orders'),
        'kitchen_refresh_seconds': forms.IntegerField(initial=0, min_value=0, max_value=300, label='Refresh interval (seconds)', help_text='0 disables automatic refresh; otherwise at least 5 seconds.'),
        'kitchen_warning_minutes': forms.IntegerField(initial=0, min_value=0, max_value=240, label='Highlight orders waiting (minutes)', help_text='0 disables highlighting. Does not advance order status.'),
        'prep_estimate_minutes': forms.IntegerField(initial=0, min_value=0, max_value=240, label='Customer preparation estimate (minutes)', help_text='0 keeps the existing menu-based estimate.'),
    },
    'notifications': {
        **{f'notify_{key}': forms.BooleanField(required=False, initial=True, label=label) for key, label in [
            ('new_order', 'New orders'), ('food_ready', 'Food ready'), ('order_served', 'Orders served'),
            ('order_completed', 'Orders completed'), ('task_assigned', 'Staff tasks'), ('general', 'General / table assignments'),
        ]},
    },
}
ALL_FIELDS = {name: field for fields in FIELDS.values() for name, field in fields.items()}
DEFAULTS = {name: field.initial if field.initial is not None else '' for name, field in ALL_FIELDS.items()}


def json_value(value):
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def normalize_values(values):
    if not isinstance(values, dict) or set(values) - ALL_FIELDS.keys():
        raise ValidationError('Unknown settings fields.')
    return {name: json_value(deepcopy(ALL_FIELDS[name]).clean(value)) for name, value in values.items()}


def validate_effective(values):
    try:
        ZoneInfo(values['timezone'])
    except (ZoneInfoNotFoundError, ValueError):
        raise ValidationError('Enter a valid IANA timezone, for example Asia/Dhaka.')
    if values['currency'] != 'BDT':
        raise ValidationError('BDT is the operational currency.')
    for options in [('allow_dine_in', 'allow_takeaway'), ('allow_pay_now', 'allow_pay_later'),
                    ('payment_cash', 'payment_card', 'payment_mobile_banking')]:
        if not any(values[key] for key in options):
            raise ValidationError('Keep at least one order type, payment timing and payment method enabled.')
    if values['enforce_hours'] and time.fromisoformat(values['opening_time']) == time.fromisoformat(values['closing_time']):
        raise ValidationError('Opening and closing times must differ; disable hours enforcement for 24-hour operation.')
    if 0 < values['kitchen_refresh_seconds'] < 5:
        raise ValidationError('Kitchen refresh must be 0 or at least 5 seconds.')
