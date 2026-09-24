import secrets

from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models


def feedback_token():
    return secrets.token_urlsafe(32)


class Reservation(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        CONFIRMED = 'CONFIRMED', 'Confirmed'
        CANCELLED = 'CANCELLED', 'Cancelled'
        COMPLETED = 'COMPLETED', 'Completed'

    restaurant = models.ForeignKey('restaurant.Restaurant', on_delete=models.CASCADE)
    branch = models.ForeignKey('restaurant.Branch', on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    phone = models.CharField(max_length=30)
    date = models.DateField()
    time = models.TimeField()
    party_size = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    note = models.TextField(blank=True, max_length=2000)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['date', 'time', 'pk']
        indexes = [models.Index(fields=['restaurant', 'branch', 'date'])]
        constraints = [models.CheckConstraint(condition=models.Q(party_size__gte=1), name='reservation_positive_party_size')]


class OrderFeedback(models.Model):
    """A bearer-token invitation, filled once after the order is served."""
    order = models.OneToOneField('orders.Order', on_delete=models.CASCADE, related_name='guest_feedback')
    token = models.CharField(max_length=43, unique=True, default=feedback_token, editable=False)
    rating = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.TextField(blank=True, max_length=2000)
    submitted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(rating__isnull=True) | models.Q(rating__gte=1, rating__lte=5), name='feedback_rating_range')]
