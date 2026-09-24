from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import render, redirect
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from restaurant.branch_services import get_active_branch
from restaurant.models import Restaurant
from .forms import SettingsForm
from .models import BranchSettings, RestaurantSettings
from .schema import SECTIONS, DEFAULTS, validate_effective
from .services import resolve_settings


@login_required
@require_http_methods(['GET', 'POST'])
def settings_page(request):
    user = request.user
    if user.role != 'owner' or not user.is_active or not user.is_active_staff or not user.restaurant_id:
        raise PermissionDenied('Only the restaurant Owner can manage Settings.')
    restaurant = user.restaurant
    branch = get_active_branch(request, restaurant)
    section = request.GET.get('section', 'business')
    scope = request.GET.get('scope', 'restaurant')
    if section not in SECTIONS or scope not in {'restaurant', 'branch'}:
        raise PermissionDenied('Invalid settings section or scope.')
    if scope == 'branch' and (branch is None or branch.restaurant_id != restaurant.pk):
        raise PermissionDenied('Select a branch first.')
    is_branch = scope == 'branch'
    model = BranchSettings if is_branch else RestaurantSettings
    lookup = {'branch': branch} if is_branch else {'restaurant': restaurant}
    existing = model.objects.filter(**lookup).values_list('values', flat=True).first() or {}
    effective = resolve_settings(restaurant, branch if is_branch else None)
    form = SettingsForm(request.POST if request.method == 'POST' else None, section=section, effective=effective,
                        overrides=existing if is_branch else None, business=None if is_branch else restaurant)
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                # Lock the parent so concurrent section edits and inheritance changes serialize.
                Restaurant.objects.select_for_update().get(pk=restaurant.pk)
                row, _ = model.objects.get_or_create(**lookup)
                row.values = form.updated_values(row.values)
                row.full_clean()
                parent = resolve_settings(restaurant)
                validate_effective({**parent, **row.values} if is_branch else {**DEFAULTS, **row.values})
                if not is_branch:
                    for overrides in BranchSettings.objects.filter(branch__restaurant=restaurant).values_list('values', flat=True):
                        validate_effective({**DEFAULTS, **row.values, **overrides})
                row.save()
                if section == 'business' and not is_branch:
                    for name in ('name', 'address', 'phone'):
                        setattr(restaurant, name, form.cleaned_data['business_' + name])
                    restaurant.save(update_fields=['name', 'address', 'phone'])
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, 'Settings saved.')
            return redirect(reverse('business_settings:settings') + f'?section={section}&scope={scope}')
    return render(request, 'business_settings/settings.html', {
        'form': form, 'sections': SECTIONS, 'section': section, 'section_title': SECTIONS[section],
        'scope': scope, 'restaurant': restaurant, 'branch': branch,
    }, status=400 if request.method == 'POST' else 200)
