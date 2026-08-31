from django.shortcuts import render, get_object_or_404, redirect
from users.decorators import kitchen_staff_required
from orders.models import Order


@kitchen_staff_required
def kitchen_dashboard(request):
    orders = Order.objects.exclude(
        status__in=[
            'SERVED',
            'COMPLETED'
        ]
    ).order_by('-created_at')

    return render(
        request,
        'kitchen/dashboard.html',
        {
            'orders': orders
        }
    )


@kitchen_staff_required
def update_order_status(request, order_id):
    order = get_object_or_404(
        Order,
        id=order_id
    )

    kitchen_transitions = {
        'NEW': 'ACCEPTED',
        'ACCEPTED': 'PREPARING',
        'PREPARING': 'READY',
    }

    if request.method == 'POST':
        next_status = kitchen_transitions.get(
            order.status
        )

        if next_status:
            order.status = next_status
            order.save(
                update_fields=['status']
            )

    return redirect(
        'kitchen_dashboard'
    )