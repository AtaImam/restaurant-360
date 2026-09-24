from django.db import models
from django.contrib.auth.models import AbstractUser

class User(AbstractUser):
    ROLE_CHOICES = [
        ('admin', 'Admin'),
        ('owner', 'Owner'),
        ('manager', 'Manager'),
        ('waiter', 'Waiter'),
        ('chief', 'Chief'),
        ('kitchen_manager', 'Kitchen Manager'),
        ('bar_manager', 'Bar Manager'),
    ]

    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default='waiter'
    )
    
    phone = models.CharField(max_length=20, blank=True, null=True)
    restaurant = models.ForeignKey(
        'restaurant.Restaurant',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='staff_members'
    )
    branch = models.ForeignKey(
        'restaurant.Branch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='staff_members'
    )
    is_active_staff = models.BooleanField(default=True)

    # Fix related_name clashes with auth.User
    groups = models.ManyToManyField(
        'auth.Group',
        verbose_name='groups',
        blank=True,
        related_name='custom_user_set',
        help_text='The groups this user belongs to.'
    )
    user_permissions = models.ManyToManyField(
        'auth.Permission',
        verbose_name='user permissions',
        blank=True,
        related_name='custom_user_set',
        help_text='Specific permissions for this user.'
    )

    class Meta:
        ordering = ['-date_joined']
        verbose_name = 'User'
        verbose_name_plural = 'Users'

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"

    def get_dashboard_url(self):
        """Get the dashboard URL based on user role"""
        role_urls = {
            'admin': '/admin/',
            'owner': '/dashboard/',
            'manager': '/dashboard/',
            'waiter': '/waiter/',
            'chief': '/kitchen/',
            'kitchen_manager': '/kitchen/',
            'bar_manager': '/dashboard/',
        }
        return role_urls.get(self.role, '/dashboard/')

    def can_access_dashboard(self):
        """Check if user can access main dashboard"""
        return self.role in ['admin', 'owner', 'manager']

    def can_access_kitchen(self):
        """Check if user can access kitchen dashboard"""
        return self.role in ['admin', 'owner', 'manager', 'chief', 'chef', 'kitchen_manager'] or self.is_superuser

    def can_operate_kitchen(self):
        """Check if user has interactive kitchen operator privileges (Chief or Kitchen Manager)."""
        return self.role in ['chief', 'chef', 'kitchen_manager']

    def can_manage_staff(self):
        """Check if user can manage other staff"""
        return self.role in ['admin', 'owner', 'manager']

    def can_view_reports(self):
        """Check if user can view reports"""
        return self.role in ['admin', 'owner', 'manager']
