from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("webhook_tg/", views.webhook_tg, name="webhook_tg"),
    path("webhook_tg/who-update-event/", views.who_update_event, name="who_update_event"),
]