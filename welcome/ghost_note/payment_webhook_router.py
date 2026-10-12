"""Compatibility for the YooKassa URL historically shared with Telegram."""
import json
from functools import wraps

from django.views.decorators.csrf import csrf_exempt

from .purchase_views import yookassa_webhook


def with_payment_webhook(telegram_view):
    @wraps(telegram_view)
    @csrf_exempt
    def dispatch(request, *args, **kwargs):
        if request.method == 'POST':
            try:
                payload = json.loads(request.body.decode('utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            if (isinstance(payload, dict) and payload.get('type') == 'notification'
                    and payload.get('event') in ('payment.succeeded', 'payment.canceled')):
                # Use the existing verifier and fulfillment transaction. Do not
                # pass payment details to Telegram's message parser/logging.
                return yookassa_webhook(request)
        return telegram_view(request, *args, **kwargs)
    return dispatch
