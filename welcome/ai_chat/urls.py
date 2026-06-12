from django.urls import path

from . import views

urlpatterns = [
    path('ai/send/', views.send_message, name='ai_send'),
    path('ai/clear/', views.clear_history, name='ai_clear'),
    path('ai/history/', views.get_history, name='ai_history'),
]
