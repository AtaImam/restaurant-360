from django.db import models
from .schema import normalize_values


class RestaurantSettings(models.Model):
    restaurant = models.OneToOneField('restaurant.Restaurant', on_delete=models.CASCADE, related_name='business_settings')
    values = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        self.values = normalize_values(self.values)


class BranchSettings(models.Model):
    branch = models.OneToOneField('restaurant.Branch', on_delete=models.CASCADE, related_name='business_settings')
    values = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        self.values = normalize_values(self.values)
