"""Shared tenant and active-branch scope for existing staff screens."""
from django.core.exceptions import PermissionDenied
from restaurant.branch_services import get_active_branch


def staff_branch(request):
    user = request.user
    if not user.is_authenticated or not user.is_active or not user.is_active_staff or not user.restaurant_id:
        raise PermissionDenied('An active staff account with a restaurant is required.')
    branch = get_active_branch(request, user.restaurant)
    if branch is not None and branch.restaurant_id != user.restaurant_id:
        raise PermissionDenied('Your branch does not belong to your restaurant.')
    return branch


def staff_queryset(request, model):
    branch = staff_branch(request)
    paths = {
        'user': ('restaurant', 'branch'),
        'employeeprofile': ('user__restaurant', 'user__branch'),
        'orderstaffservice': ('order__restaurant', 'order__branch'),
    }
    restaurant_path, branch_path = paths.get(model._meta.model_name, ('restaurant', 'branch'))
    return model.objects.filter(**{
        restaurant_path + '_id': request.user.restaurant_id,
        branch_path: branch,
    })


def ensure_staff_record_access(actor, restaurant_id, branch_id):
    if not actor.is_authenticated or not actor.is_active or not actor.is_active_staff:
        raise PermissionDenied('An active staff account is required.')
    if not actor.restaurant_id or actor.restaurant_id != restaurant_id:
        raise PermissionDenied('This record belongs to another restaurant.')
    if actor.role not in {'owner', 'admin'} and not actor.is_superuser and actor.branch_id != branch_id:
        raise PermissionDenied('This record belongs to another branch.')
