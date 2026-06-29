import requests

from env import OWNER_CHAT_ID
from webhook_tg.telegram import tg_send_message


def notify_owner(text: str) -> None:
    if not text:
        return
    try:
        from env import OWNER_NOTIFY_URL, WHO_UPDATE_EVENT_TOKEN
    except ImportError:
        OWNER_NOTIFY_URL = ""
        WHO_UPDATE_EVENT_TOKEN = ""

    if OWNER_NOTIFY_URL and WHO_UPDATE_EVENT_TOKEN:
        try:
            resp = requests.post(
                OWNER_NOTIFY_URL,
                json={"text": text},
                headers={"X-Who-Update-Token": WHO_UPDATE_EVENT_TOKEN},
                timeout=10,
            )
            if resp.status_code == 200:
                return
        except Exception:
            pass

    try:
        tg_send_message(OWNER_CHAT_ID, text)
    except Exception:
        pass
