# Restaurant 360

Restaurant 360 is a Django-based restaurant management system with role-based access control, landing page, secure login, and dedicated interfaces for different staff roles.

## Features

- Modern landing page
- Secure login system
- Role-based dashboard flow
- Staff access control by role
- Kitchen and dashboard modules
- Restaurant and order management

## User Roles

- Admin
- Owner
- Manager
- Waiter
- Chef
- Kitchen Manager
- Bar Manager

## Default Login Credentials

| Role | Username | Password |
|------|----------|----------|
| Admin | admin | admin123 |
| Owner | owner | password123 |
| Manager | manager | password123 |
| Waiter | waiter | password123 |
| Chef | chief | password123 |
| Kitchen Manager | kitchenmgr | password123 |
| Bar Manager | barmgr | password123 |

## Run the Project

1. Open terminal in the project folder.
2. Install requirements:

```bash
pip install -r requirements.txt
```

3. Apply migrations:

```bash
python manage.py migrate
```

4. Start the server:

```bash
python manage.py runserver 0.0.0.0:8000
```

5. Open the app:

- http://localhost:8000/
- http://localhost:8000/auth/
- http://localhost:8000/auth/login/

## App Flow

- Landing page: `/auth/`
- Login page: `/auth/login/`
- Role redirect: `/auth/dashboard/`
- Logout: `/auth/logout/`

After login, the user is redirected to the correct dashboard based on role:

- Admin → Django admin or system dashboard
- Owner → `/dashboard/`
- Manager → `/dashboard/`
- Waiter → waiter dashboard
- Chief → kitchen dashboard
- Kitchen Manager → kitchen dashboard
- Bar Manager → bar dashboard

## Project Structure

```text
restaurant-360/
├── config/
├── dashboard/
├── kitchen/
├── menu/
├── orders/
├── restaurant/
├── templates/
├── users/
├── manage.py
├── requirements.txt
├── create_test_users.py
├── README.md
└── db.sqlite3
```

## Authentication Setup

The project uses a custom Django user model with a `role` field. Authentication and access rules are handled in:

- `users/models.py`
- `users/views.py`
- `users/decorators.py`
- `users/forms.py`
- `config/settings.py`

## Important Notes

- The root URL redirects to the auth landing page.
- Logout redirects to the login page.
- Landing page and login page do not auto-redirect authenticated users.
- Role-based access is enforced in protected views.

## Development Notes

To create test users:

```bash
python create_test_users.py
```

## Support

Use the app as a local development project for restaurant operations, kitchen task tracking, and staff role management.
