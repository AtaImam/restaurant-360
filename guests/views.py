from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Avg
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from restaurant.branch_services import get_active_branch
from restaurant.models import Table
from .forms import FeedbackForm, ReservationForm
from .models import OrderFeedback, Reservation
from .services import FEEDBACK_STATUSES


def dashboard_scope(request, roles):
    user = request.user
    if user.role not in roles or not user.restaurant_id:
        raise PermissionDenied
    branch = get_active_branch(request, user.restaurant)
    if branch is None or branch.restaurant_id != user.restaurant_id:
        raise PermissionDenied
    return user.restaurant, branch


@never_cache
@require_http_methods(['GET', 'POST'])
def reserve(request, restaurant_id, table_id):
    table = get_object_or_404(Table.objects.select_related('restaurant', 'branch'), pk=table_id,
                              restaurant_id=restaurant_id, is_active=True,
                              branch__is_active=True, branch__restaurant_id=restaurant_id)
    form = ReservationForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        reservation = form.save(commit=False)
        reservation.restaurant = table.restaurant
        reservation.branch = table.branch
        reservation.save()
        return redirect(reverse_reservation(table) + '?submitted=1')
    return render(request, 'guests/reserve.html', {'form': form, 'table': table,
                  'submitted': request.GET.get('submitted') == '1'}, status=400 if request.method == 'POST' else 200)


def reverse_reservation(table):
    from django.urls import reverse
    return reverse('guests:reserve', args=[table.restaurant_id, table.pk])


@login_required
@require_http_methods(['GET'])
def reservations(request):
    restaurant, branch = dashboard_scope(request, {'owner', 'manager'})
    rows = Reservation.objects.filter(restaurant=restaurant, branch=branch)
    return render(request, 'guests/reservations.html', {'branch': branch,
                  'page_obj': Paginator(rows, 50).get_page(request.GET.get('page'))})


@login_required
@require_POST
def reservation_action(request, pk):
    restaurant, branch = dashboard_scope(request, {'owner', 'manager'})
    with transaction.atomic():
        reservation = get_object_or_404(Reservation.objects.select_for_update(), pk=pk,
                                       restaurant=restaurant, branch=branch)
        transitions = {
            'confirm': ({'PENDING'}, 'CONFIRMED'),
            'cancel': ({'PENDING', 'CONFIRMED'}, 'CANCELLED'),
            'complete': ({'CONFIRMED'}, 'COMPLETED'),
        }
        allowed, target = transitions.get(request.POST.get('action'), (set(), None))
        if reservation.status not in allowed:
            return HttpResponse('Invalid reservation transition.', status=400)
        reservation.status = target
        reservation.save(update_fields=['status', 'updated_at'])
    return redirect('guests:reservations')


@never_cache
@require_http_methods(['GET', 'POST'])
def feedback(request, token):
    invitation = get_object_or_404(OrderFeedback.objects.select_related('order__restaurant', 'order__branch'), token=token)
    if invitation.order.status not in FEEDBACK_STATUSES:
        return HttpResponse('Feedback is available after your order is served or completed.', status=403)

    if request.method == 'POST':
        if invitation.rating is not None:
            return HttpResponse('Feedback has already been submitted for this order.', status=409)
        form = FeedbackForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                updated = OrderFeedback.objects.filter(pk=invitation.pk, rating__isnull=True).update(
                    rating=form.cleaned_data['rating'],
                    comment=form.cleaned_data['comment'],
                    submitted_at=timezone.now(),
                )
                if not updated:
                    return HttpResponse('Feedback has already been submitted for this order.', status=409)
            return redirect('guests:feedback', token=token)
    else:
        form = FeedbackForm()

    response = render(
        request,
        'guests/feedback.html',
        {
            'form': form,
            'submitted': invitation.rating is not None,
            'restaurant': invitation.order.restaurant,
            'order': invitation.order,
        },
        status=400 if request.method == 'POST' else 200,
    )
    response['Referrer-Policy'] = 'same-origin'
    return response


@login_required
@require_http_methods(['GET'])
def feedback_list(request):
    restaurant, branch = dashboard_scope(request, {'owner'})
    rows = OrderFeedback.objects.filter(
        order__restaurant=restaurant,
        order__branch=branch,
        rating__isnull=False,
    ).select_related('order', 'order__branch').order_by('-submitted_at', '-pk')
    return render(
        request,
        'guests/feedback_list.html',
        {
            'branch': branch,
            'average_rating': rows.aggregate(value=Avg('rating'))['value'],
            'feedback_count': rows.count(),
            'page_obj': Paginator(rows, 50).get_page(request.GET.get('page')),
        },
    )
