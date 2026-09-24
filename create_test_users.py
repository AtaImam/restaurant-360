#!/usr/bin/env python
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from users.models import User
from restaurant.models import Restaurant

# Create a test restaurant
restaurant, _ = Restaurant.objects.get_or_create(
    name='Test Restaurant',
    defaults={'address': '123 Main St', 'phone': '555-1234'}
)

# Create test users with different roles
admin_user, admin_created = User.objects.get_or_create(
    username='admin',
    defaults={
        'email': 'admin@test.com',
        'first_name': 'Admin',
        'last_name': 'User',
        'role': 'admin',
        'restaurant': restaurant,
        'is_staff': True,
        'is_superuser': True,
        'is_active': True,
    }
)
if admin_created:
    admin_user.set_password('admin123')
    admin_user.save()
    print('✓ Created admin: admin (password: admin123)')
else:
    print('✓ admin already exists: admin')

test_users = [
    ('owner', 'owner@test.com', 'Owner', 'User'),
    ('manager', 'manager@test.com', 'Manager', 'User'),
    ('waiter', 'waiter@test.com', 'Waiter', 'User'),
    ('chief', 'chief@test.com', 'Chief', 'Cook'),
    ('kitchen_manager', 'kitchenmgr@test.com', 'Kitchen', 'Manager'),
    ('bar_manager', 'barmgr@test.com', 'Bar', 'Manager'),
]

for role, email, first_name, last_name in test_users:
    username = email.split('@')[0]
    user, created = User.objects.get_or_create(
        username=username,
        defaults={
            'email': email,
            'first_name': first_name,
            'last_name': last_name,
            'role': role,
            'restaurant': restaurant,
            'is_staff': role in ['admin', 'owner', 'manager'],
            'is_active': True,
        }
    )
    if created:
        user.set_password('password123')
        user.save()
        print(f'✓ Created {role}: {username} (password: password123)')
    else:
        print(f'✓ {role} already exists: {username}')

print('\n✓ Test users created successfully!')
print('\nTest Credentials:')
print('Admin: admin / admin123')
print('Owner: owner / password123')
print('Manager: manager / password123')
print('Waiter: waiter / password123')
print('Chef: chief / password123')
print('Kitchen Mgr: kitchenmgr / password123')
print('Bar Mgr: barmgr / password123')
