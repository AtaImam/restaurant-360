from functools import wraps
from django.shortcuts import redirect
from django.contrib import messages

def login_required_custom(view_func):
    """Decorator to check if user is logged in"""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            messages.warning(request, 'Please log in to access this page.')
            return redirect('users:login')
        return view_func(request, *args, **kwargs)
    return wrapper

def role_required(*allowed_roles):
    """Decorator to check if user has required role"""
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                messages.warning(request, 'Please log in to access this page.')
                return redirect('users:login')

            if request.user.role not in allowed_roles:
                messages.error(request, 'You do not have permission to access this page.')
                return redirect('users:landing')

            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator

def owner_required(view_func):
    """Decorator to check if user is owner"""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            messages.warning(request, 'Please log in to access this page.')
            return redirect('users:login')

        if request.user.role not in ['admin', 'owner']:
            messages.error(request, 'Only owners can access this page.')
            return redirect('users:landing')

        return view_func(request, *args, **kwargs)
    return wrapper

def kitchen_staff_required(view_func):
    """Decorator to check if user is kitchen staff"""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            messages.warning(request, 'Please log in to access this page.')
            return redirect('users:login')

        if request.user.role not in ['admin', 'chief', 'kitchen_manager']:
            messages.error(request, 'Only kitchen staff can access this page.')
            return redirect('users:landing')

        return view_func(request, *args, **kwargs)
    return wrapper

def manager_required(view_func):
    """Decorator to check if user is manager or owner"""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            messages.warning(request, 'Please log in to access this page.')
            return redirect('users:login')

        if request.user.role not in ['admin', 'owner', 'manager']:
            messages.error(request, 'You do not have permission to access this page.')
            return redirect('users:landing')

        return view_func(request, *args, **kwargs)
    return wrapper
