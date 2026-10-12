from __future__ import annotations

import hashlib
import hmac
import logging

import requests
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from env import TOKEN_BOT

logger = logging.getLogger(__name__)

PRIMARY_WEBHOOK_URL = "https://maksonchik.ru/webhook_tg/"
MAX_UPDATE_BYTES = 2 * 1024 * 1024
_session = requests.Session()


def _webhook_secret() -> str:
    return hashlib.sha256(f"who-update:{TOKEN_BOT}".encode()).hexdigest()


@csrf_exempt
def who_update_relay(request: HttpRequest):
    """Принимает Telegram webhook в YC и без изменений передаёт его основной очереди."""
    if request.method != "POST":
        return HttpResponse(status=405)

    supplied_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    secret = _webhook_secret()
    if not hmac.compare_digest(supplied_secret, secret):
        return HttpResponse("Invalid Telegram webhook secret", status=403)

    body = request.body
    if not body or len(body) > MAX_UPDATE_BYTES:
        return JsonResponse({"ok": False, "error": "invalid body size"}, status=400)

    try:
        response = _session.post(
            PRIMARY_WEBHOOK_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": secret,
            },
            timeout=(2, 5),
        )
    except requests.RequestException as exc:
        logger.warning("WhoUpdate relay upstream connection failed: %s", type(exc).__name__)
        return JsonResponse({"ok": False, "error": "upstream unavailable"}, status=502)

    if not 200 <= response.status_code < 300:
        logger.warning("WhoUpdate relay upstream status=%s", response.status_code)
        return JsonResponse({"ok": False, "error": "upstream rejected update"}, status=502)

    return JsonResponse({"ok": True})
