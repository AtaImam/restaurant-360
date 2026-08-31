from django.shortcuts import render, get_object_or_404, redirect
from users.decorators import kitchen_staff_required
from orders.models import Order


@kitchen_staff_required
def kitchen_dashboard(request):
    orders = Order.objects.exclude(
        status__in=['SERVED', 'COMPLETED']
    ).order_by('-created_at')

    return render(request, 'kitchen/dashboard.html', {
        'orders': orders
    })


@kitchen_staff_required
def update_order_status(request, order_id):
    order = get_object_or_404(Order, id=order_id)

    if request.method == 'POST':

        if order.status == 'NEW':
            order.status = 'ACCEPTED'

        elif order.status == 'ACCEPTED':
            order.status = 'PREPARING'

        elif order.status == 'PREPARING':
            order.status = 'READY'

        order.save()

    return redirect('kitchen_dashboard')