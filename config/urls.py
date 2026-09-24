from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView


urlpatterns = [
    path("", RedirectView.as_view(url="auth/", permanent=False)),
    path("auth/", include("users.urls", namespace="users")),

    path("admin/", admin.site.urls),
    path("menu/", include("menu.urls")),
    path("kitchen/", include("kitchen.urls")),
    path("dashboard/", include("dashboard.urls")),
    path("waiter/", include("staff.waiter_urls", namespace="waiter")),

    path("orders/", include("orders.urls")),
    path("inventory/", include("inventory.urls")),
    path("menu-management/", include("menu.management_urls")),
    path("pos/", include("pos.urls")),
    path("staff/", include("staff.urls")),
    path("finance/", include("finance.urls")),
    path("guests/", include("guests.urls")),
    path("settings/", include("business_settings.urls")),
    path(
        "restaurant-management/",
        include("restaurant.urls", namespace="restaurant"),
    ),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)