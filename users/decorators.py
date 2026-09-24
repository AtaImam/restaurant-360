from functools import wraps
from django.shortcuts import redirect
from django.contrib import messages

# ---------------------------------------------------------------------------
# Development note:
# All decorators below pass superusers (is_superuser=True) unconditionally.
# Role-based enforcement is preserved in the logic but skipped for superusers.
# This lets the owner/admin account access every implemented page during dev.
# Re-enable strict role enforcement later by removing the superuser bypasses.
# ---------------------------------------------------------------------------


def login_required_custom(view_func):
    """Decorator to check if user is logged in."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            messages.warning(request, 'Please log in to access this page.')
            return redirect('users:login')
        return view_func(request, *args, **kwargs)
    return wrapper


def role_required(*allowed_roles):
    """
    Decorator to check if user has one of the required roles.
    Superusers always pass regardless of their role field.
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                messages.warning(request, 'Please log in to access this page.')
                return redirect('users:login')

            # Superuser bypass — full access during development
            if request.user.is_superuser:
                return view_func(request, *args, **kwargs)

            if request.user.role not in allowed_roles:
                messages.error(request, 'You do not have permission to access this page.')
                return redirect('users:landing')

            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator


def owner_required(view_func):
    """
    Decorator to check if user is owner/admin.
    Superusers always pass.
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            messages.warning(request, 'Please log in to access this page.')
            return redirect('users:login')

        # Superuser bypass
        if request.user.is_superuser:
            return view_func(request, *args, **kwargs)

        if request.user.role not in ['admin', 'owner']:
            messages.error(request, 'Only owners can access this page.')
            return redirect('users:landing')

        return view_func(request, *args, **kwargs)
    return wrapper


def kitchen_staff_required(view_func):
    """
    Decorator to check if user is kitchen staff.
    Superusers always pass.
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            messages.warning(request, 'Please log in to access this page.')
            return redirect('users:login')

        # Superuser bypass
        if request.user.is_superuser:
            return view_func(request, *args, **kwargs)

        if request.user.role not in ['admin', 'chief', 'kitchen_manager']:
            messages.error(request, 'Only kitchen staff can access this page.')
            return redirect('users:landing')

        return view_func(request, *args, **kwargs)
    return wrapper


def manager_required(view_func):
    """
    Decorator to check if user is manager or above.
    Superusers always pass.
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            messages.warning(request, 'Please log in to access this page.')
            return redirect('users:login')

        # Superuser bypass
        if request.user.is_superuser:
            return view_func(request, *args, **kwargs)

        if request.user.role not in ['admin', 'owner', 'manager']:
            messages.error(request, 'You do not have permission to access this page.')
            return redirect('users:landing')

        return view_func(request, *args, **kwargs)
    return wrapper
