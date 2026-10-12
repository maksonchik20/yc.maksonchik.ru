import logging

import requests

from env import TOKEN_BOT
from .inner_models.BusinessConnection import BusinessConnection
from .bot_outgoing_log import log_bot_outgoing

logger = logging.getLogger(__name__)

# DNS api.telegram.org с Timeweb/YC часто резолвится в недоступный IP.
# Ходим напрямую на проверенный DC с Host: api.telegram.org.
TELEGRAM_API_ENDPOINTS = (
    {"base": "https://149.154.167.220", "host": "api.telegram.org", "verify": False},
    {"base": "https://149.154.167.99", "host": "api.telegram.org", "verify": False},
    {"base": "https://api.telegram.org", "host": None, "verify": True},
)
TELEGRAM_API_TIMEOUT = 8

# Для тестов/обратной совместимости: канонический URL без обхода DNS.
api_tg_url = f"https://api.telegram.org/bot{TOKEN_BOT}"


def _telegram_post(method: str, *, json=None, data=None, files=None, timeout: int = TELEGRAM_API_TIMEOUT):
    """POST к Bot API через список endpoint'ов (IP fallback)."""
    last_error = None
    for endpoint in TELEGRAM_API_ENDPOINTS:
        url = f"{endpoint['base'].rstrip('/')}/bot{TOKEN_BOT}/{method}"
        headers = {}
        host = endpoint.get("host")
        if host:
            headers["Host"] = host
        try:
            response = requests.post(
                url,
                json=json,
                data=data,
                files=files,
                headers=headers or None,
                timeout=timeout,
                verify=endpoint.get("verify", True),
            )
            return response
        except requests.RequestException as exc:
            last_error = exc
            logger.warning("Telegram API %s via %s failed: %s", method, endpoint["base"], exc)
    if last_error:
        raise last_error
    raise RuntimeError("Telegram API недоступен")


def dispatch_telegram_request(method: str, chat_id, payload: dict, timeout: int = 5):
    if not chat_id:
        return False, "empty chat_id"

    body = {"chat_id": chat_id, **payload}
    try:
        response = _telegram_post(method, json=body, timeout=timeout)
    except requests.RequestException as exc:
        logger.error("%s failed chat_id=%s: %s", method, chat_id, exc)
        return False, str(exc)

    try:
        result = response.json()
    except ValueError:
        error = f"invalid JSON status={response.status_code} body={response.text[:200]}"
        logger.error("%s %s chat_id=%s", method, error, chat_id)
        return False, error

    if not result.get("ok"):
        error = str(result.get("description") or result)
        logger.error("%s API error chat_id=%s status=%s response=%s", method, chat_id, response.status_code, result)
        return False, error

    log_bot_outgoing(chat_id=chat_id, method=method)
    return True, ""


def tg_send_message(chat_id: str, text: str, timeout: int = 5) -> bool:
    if not chat_id:
        return False
    if text is None:
        return False

    ok, _ = dispatch_telegram_request(
        "sendMessage",
        chat_id,
        {
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=timeout,
    )
    return ok


def get_business_connection(msg) -> BusinessConnection:
    body = {"business_connection_id": msg.get("business_connection_id")}
    response = _telegram_post("getBusinessConnection", json=body, timeout=TELEGRAM_API_TIMEOUT)
    ans = response.json()
    print("business_connection", ans)
    result = ans.get("result") or {}
    user = result.get("user") or {}
    return BusinessConnection(
        user_chat_id=result.get("user_chat_id"),
        user_id=user.get("id"),
        username=user.get("username"),
    )


def _media_payload(caption: str) -> dict:
    payload = {}
    if caption:
        payload["caption"] = caption
        payload["parse_mode"] = "HTML"
        payload["disable_web_page_preview"] = True
    return payload


def send_photo(chat_id, photo_id: str, caption: str = "", timeout: int = 5) -> bool:
    """Отправка фото по file_id. caption — подпись к фото (HTML)."""
    if not chat_id or not photo_id:
        return False
    payload = {"photo": photo_id, **_media_payload(caption)}
    ok, _ = dispatch_telegram_request("sendPhoto", chat_id, payload, timeout=timeout)
    return ok


def send_audio(chat_id, audio_file_id: str, caption: str = "", timeout: int = 5) -> bool:
    """Отправка аудио/голоса по file_id."""
    if not chat_id or not audio_file_id:
        return False
    payload = {"audio": audio_file_id, **_media_payload(caption)}
    ok, _ = dispatch_telegram_request("sendAudio", chat_id, payload, timeout=timeout)
    return ok


def send_video(chat_id, video_file_id: str, caption: str = "", timeout: int = 5) -> bool:
    """Отправка видео по file_id."""
    if not chat_id or not video_file_id:
        return False
    payload = {"video": video_file_id, **_media_payload(caption)}
    ok, _ = dispatch_telegram_request("sendVideo", chat_id, payload, timeout=timeout)
    return ok


def send_document(chat_id, document_file_id: str, caption: str = "", timeout: int = 5) -> bool:
    """Отправка документа по file_id."""
    if not chat_id or not document_file_id:
        return False
    payload = {"document": document_file_id, **_media_payload(caption)}
    ok, _ = dispatch_telegram_request("sendDocument", chat_id, payload, timeout=timeout)
    return ok


def send_photo_bytes(chat_id, image_bytes: bytes, caption: str = "", timeout: int = 60) -> bool:
    if not chat_id or not image_bytes:
        return False
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    files = {"photo": ("chart.png", image_bytes, "image/png")}

    last_exc = None
    for attempt in range(3):
        try:
            response = _telegram_post("sendPhoto", data=data, files=files, timeout=timeout)
            result = response.json()
            if result.get("ok"):
                log_bot_outgoing(chat_id=chat_id, method="sendPhoto")
                return True
            logger.error("sendPhoto bytes API error chat_id=%s response=%s", chat_id, result)
            return False
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            logger.warning(
                "sendPhoto bytes attempt %s failed chat_id=%s: %s",
                attempt + 1,
                chat_id,
                exc,
            )
    logger.error("sendPhoto bytes failed chat_id=%s: %s", chat_id, last_exc)
    return False
