import json
import logging

from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .telegram_bot import process_telegram_update

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def ghost_telegram_webhook(request):
    if not getattr(settings, 'GHOST_NOTE_BOT_TOKEN', ''):
        return HttpResponse(status=503)

    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return HttpResponse(status=400)

    try:
        process_telegram_update(payload)
    except Exception:
        logger.exception('Telegram webhook processing failed')

    return HttpResponse('ok')
