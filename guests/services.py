import base64
from io import BytesIO

import qrcode
from django.conf import settings
from django.urls import reverse

from restaurant.branch_services import get_active_branch
from .models import OrderFeedback


FEEDBACK_STATUSES = {'SERVED', 'COMPLETED'}


def remember_qr_order(request, order):
    # Only checkout grants this browser access; public order-id pages never do.
    ids = request.session.get('qr_feedback_orders', [])
    request.session['qr_feedback_orders'] = (ids + [order.pk])[-100:]


def qr_feedback_url(request, order):
    if order.pk not in request.session.get('qr_feedback_orders', []) or order.status not in FEEDBACK_STATUSES:
        return None
    feedback, _ = OrderFeedback.objects.get_or_create(order=order)
    if feedback.rating is not None:
        return None
    return reverse('guests:feedback', args=[feedback.token])


def get_order_feedback_url(request, feedback):
    site_url = getattr(settings, 'SITE_URL', '').strip().rstrip('/')
    path = reverse('guests:feedback', args=[feedback.token])
    if site_url:
        if not site_url.startswith(('http://', 'https://')):
            site_url = f"http://{site_url}"
        return f"{site_url}{path}"
    if request is not None:
        return request.build_absolute_uri(path)
    return path


def bill_feedback_context(request, order):
    if order.status == 'CANCELLED':
        return {}
    user = getattr(request, 'user', None)
    is_staff = user and user.is_authenticated and (user.restaurant_id == order.restaurant_id or user.is_superuser)
    from orders.services import can_customer_access_order
    is_customer = can_customer_access_order(request, order)
    if not (is_staff or is_customer):
        return {}
    if is_staff and order.branch_id:
        branch = get_active_branch(request, user.restaurant)
        if branch is not None and branch.pk != order.branch_id:
            return {}
    feedback, _ = OrderFeedback.objects.get_or_create(order=order)
    if feedback.rating is not None:
        return {}
    url = get_order_feedback_url(request, feedback)
    buffer = BytesIO()
    qrcode.make(url).save(buffer, format='PNG')
    return {'feedback_url': url, 'feedback_qr': base64.b64encode(buffer.getvalue()).decode('ascii')}
