from django.shortcuts import render, redirect
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_http_methods
from django.db.models import Q
from .models import User
from .forms import LoginForm
from restaurant.models import Restaurant

def landing(request):
    """Landing page view"""
    # Public landing page must always show the guest experience.
    # Do not expose dashboard/profile links on the public home page.
    context = {
        'total_restaurants': Restaurant.objects.count(),
        'total_users': User.objects.filter(is_active=True).count(),
        'is_authenticated': False,
    }
    return render(request, 'auth/landing.html', context)

@require_http_methods(["GET", "POST"])
def login_view(request):
    """Login view"""
    # On GET, show login form even if authenticated
    # On POST, process login
    
    if request.method == 'POST':
        form = LoginForm(request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            messages.success(request, f'Welcome back, {user.get_full_name() or user.username}!')
            next_url = request.GET.get('next', user.get_dashboard_url())
            return redirect(next_url)
    else:
        form = LoginForm()
    
    context = {
        'form': form,
        'roles': User.ROLE_CHOICES,
        'is_authenticated': request.user.is_authenticated,
    }
    return render(request, 'auth/login.html', context)

@login_required(login_url='users:login')
@require_http_methods(["POST"])
def logout_view(request):
    """Logout view"""
    logout(request)
    return redirect('users:login')

@login_required(login_url='users:login')
def profile_view(request):
    """User profile view"""
    context = {
        'user': request.user,
    }
    return render(request, 'auth/profile.html', context)

def role_dashboard_redirect(request):
    """Redirect to appropriate dashboard based on user role"""
    if not request.user.is_authenticated:
        return redirect('users:login')
    return redirect(request.user.get_dashboard_url())
