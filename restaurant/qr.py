"""Canonical public URLs for printed table QR codes."""
from ipaddress import ip_address
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.validators import URLValidator
from django.urls import reverse


def qr_base_url():
    value = getattr(settings, "SITE_URL", "").strip().rstrip("/")
    try:
        URLValidator(schemes=["http", "https"])(value)
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname.lower()
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
            raise ValueError
        if host == "localhost" or host.endswith(".localhost"):
            raise ValueError
        try:
            address = ip_address(host)
        except ValueError:
            address = None
        if address and (address.is_loopback or address.is_unspecified):
            raise ValueError
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
    except (ValidationError, ValueError, AttributeError):
        raise ImproperlyConfigured(
            "Set SITE_URL to a phone-reachable HTTP(S) origin, e.g. "
            "http://restaurant-demo.local:8000 or https://orders.example.com. "
            "Do not use localhost, a loopback/bind address, credentials, a path, query or fragment."
        ) from None
    return value


def table_menu_url(table):
    return qr_base_url() + reverse(
        "customer_menu", kwargs={"restaurant_id": table.restaurant_id, "table_id": table.pk},
    )
