import json
import os
import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from webhook_tg.models import WhoUpdateBotEvent
from webhook_tg.owner_notify import notify_owner


def in_monitoring_window(now):
    """12:00–01:59 по Europe/Moscow."""
    hour = now.hour
    return hour >= 12 or hour <= 1


class Command(BaseCommand):
    help = "Alert OWNER if no WhoUpdateBot events in the last hour (12:00–01:59 MSK)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--test-notify",
            action="store_true",
            help="Send a test notification to OWNER_CHAT_ID",
        )
        parser.add_argument(
            "--cooldown",
            type=int,
            default=3600,
            help="Min seconds between repeated silence alerts (default 3600)",
        )
        parser.add_argument(
            "--state-file",
            default="/var/tmp/who_update_silence_state.json",
            help="Cooldown state file",
        )

    def handle(self, *args, **opts):
        if opts["test_notify"]:
            notify_owner(
                "🧪 Тест: мониторинг WhoUpdateBot подключён (yc.maksonchik.ru)",
            )
            self.stdout.write("test notification sent")
            return

        now = timezone.localtime()
        if not in_monitoring_window(now):
            self.stdout.write(f"skipped: outside window ({now:%H:%M} MSK)")
            return

        threshold = now - timedelta(hours=1)
        if WhoUpdateBotEvent.objects.filter(received_at__gte=threshold).exists():
            self.stdout.write(f"OK: events since {threshold:%H:%M}")
            return

        cooldown = opts["cooldown"]
        state_file = opts["state_file"]
        now_ts = int(time.time())
        state = {}
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
        except FileNotFoundError:
            state = {}
        except Exception:
            state = {}

        last_alert = int(state.get("last_alert", 0))
        if now_ts - last_alert < cooldown:
            self.stdout.write(f"skipped: cooldown ({now_ts - last_alert}s < {cooldown}s)")
            return

        last_event = WhoUpdateBotEvent.objects.order_by("-received_at").first()
        if last_event:
            last_str = timezone.localtime(last_event.received_at).strftime("%d.%m.%Y %H:%M:%S")
        else:
            last_str = "нет данных"

        text = (
            "⚠️ WhoUpdateBot: за последний час нет новых сообщений.\n"
            f"Последнее событие: {last_str} (MSK)"
        )
        notify_owner(text)

        state["last_alert"] = now_ts
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f)

        self.stdout.write("silence alert sent")
