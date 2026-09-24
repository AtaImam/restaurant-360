from django.urls import path
from . import views

app_name = 'guests'
urlpatterns = [
    path('reserve/<int:restaurant_id>/table/<int:table_id>/', views.reserve, name='reserve'),
    path('feedback/<str:token>/', views.feedback, name='feedback'),
    path('dashboard/reservations/', views.reservations, name='reservations'),
    path('dashboard/reservations/<int:pk>/', views.reservation_action, name='reservation_action'),
    path('dashboard/feedback/', views.feedback_list, name='feedback_list'),
]
