from django.shortcuts import render
from django.utils import timezone
from django.db.models import Sum, Avg

from orders.models import Order


def owner_dashboard(request):
    today = timezone.localdate()

    today_orders = Order.objects.filter(
        created_at__date=today
    )

    today_revenue = today_orders.aggregate(
        total=Sum('total_amount')
    )['total'] or 0

    total_orders = today_orders.count()

    active_orders = Order.objects.filter(
        status__in=[
            'NEW',
            'ACCEPTED',
            'PREPARING',
            'READY'
        ]
    ).count()

    average_order_value = today_orders.aggregate(
        average=Avg('total_amount')
    )['average'] or 0

    recent_orders = Order.objects.select_related(
        'table'
    ).order_by('-created_at')[:5]

    context = {
        'today_revenue': today_revenue,
        'total_orders': total_orders,
        'active_orders': active_orders,
        'average_order_value': average_order_value,
        'recent_orders': recent_orders,
    }

    return render(
        request,
        'dashboard/index.html',
        context
    )