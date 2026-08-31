from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User

@admin.register(User)
class UserAdmin(BaseUserAdmin):
    fieldsets = BaseUserAdmin.fieldsets + (
        ('Restaurant Staff Info', {
            'fields': ('role', 'phone', 'restaurant', 'is_active_staff')
        }),
    )
    
    list_display = ('username', 'email', 'get_full_name', 'role', 'restaurant', 'is_active')
    list_filter = ('role', 'is_active', 'restaurant', 'date_joined')
    search_fields = ('username', 'email', 'first_name', 'last_name')
    
    def get_full_name(self, obj):
        return obj.get_full_name() or obj.username
    get_full_name.short_description = 'Full Name'
