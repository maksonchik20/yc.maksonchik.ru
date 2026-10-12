from django.urls import path
from ghost_note.payment_webhook_router import with_payment_webhook
from . import telegram_relay, views

urlpatterns = [
    path("", views.index, name="index"),
    path(
        "yandex_1cf6644d705ed152.html",
        views.yandex_webmaster_verify,
        name="yandex_webmaster_verify",
    ),
    path("webhook_tg/", with_payment_webhook(views.webhook_tg), name="webhook_tg"),
    path("webhook_tg/who-update-event/", views.who_update_event, name="who_update_event"),
    path("webhook_tg/who-update-relay/", telegram_relay.who_update_relay, name="who_update_relay"),
]
