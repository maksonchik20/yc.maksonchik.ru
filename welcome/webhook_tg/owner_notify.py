import requests

from env import OWNER_CHAT_ID, WHO_UPDATE_EVENT_TOKEN
from webhook_tg.telegram import tg_send_message


def notify_owner(text: str) -> None:
    if not text:
        return
    try:
        from env import OWNER_NOTIFY_URL
    except ImportError:
        OWNER_NOTIFY_URL = ""

    if OWNER_NOTIFY_URL and WHO_UPDATE_EVENT_TOKEN:
        try:
            resp = requests.post(
                OWNER_NOTIFY_URL,
                json={"text": text},
                headers={"X-Who-Update-Token": WHO_UPDATE_EVENT_TOKEN},
                timeout=5,
            )
            if resp.status_code == 200:
                return
        except Exception:
            pass

    tg_send_message(OWNER_CHAT_ID, text)
