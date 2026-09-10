from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import render, get_object_or_404, redirect
from django.views.decorators.http import require_POST

from orders.services import orders_for_user, transition_order_status


def kitchen_dashboard(request):
    orders = orders_for_user(request.user).exclude(
        status__in=[
            'SERVED',
            'COMPLETED'
        ]
    ).select_related('table').prefetch_related('items__menu_item').order_by('-created_at')

    return render(
        request,
        'kitchen/dashboard.html',
        {
            'orders': orders
        }
    )


@require_POST
def update_order_status(request, order_id):
    order = get_object_or_404(
        orders_for_user(request.user),
        id=order_id
    )

    try:
        transition_order_status(
            order,
            request.POST.get('status'),
            allowed_targets={'ACCEPTED', 'PREPARING', 'READY'},
        )
    except ValidationError as error:
        messages.error(request, ' '.join(error.messages))

    return redirect(
        'kitchen_dashboard'
    )
