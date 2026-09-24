from django.urls import path
from .views import settings_page
app_name = 'business_settings'
urlpatterns = [path('', settings_page, name='settings')]
