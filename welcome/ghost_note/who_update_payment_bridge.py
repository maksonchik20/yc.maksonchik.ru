import logging

import requests


logger = logging.getLogger(__name__)
WHO_UPDATE_WEBHOOK_URL = "https://maksonchik.ru/bot/yookassa/webhook/"


def _bridge_token():
    try:
        from env import WHO_UPDATE_PAYMENT_WEBHOOK_TOKEN
    except ImportError:
        return ""
    return str(WHO_UPDATE_PAYMENT_WEBHOOK_TOKEN or "").strip()


def forward_who_update_payment(payload):
    payment = payload.get("object") or {}
    metadata = payment.get("metadata") or {}
    if metadata.get("service") != "who_update":
        return None

    token = _bridge_token()
    if not token:
        logger.error("WHO_UPDATE_PAYMENT_WEBHOOK_TOKEN is not configured")
        return 503
    try:
        response = requests.post(
            WHO_UPDATE_WEBHOOK_URL,
            json=payload,
            headers={"X-Who-Update-Payment-Token": token},
            timeout=15,
        )
    except requests.RequestException:
        logger.exception("WhoUpdate payment webhook forwarding failed")
        return 503
    if response.status_code >= 500:
        logger.error("WhoUpdate payment webhook returned %s", response.status_code)
        return 503
    return 200
