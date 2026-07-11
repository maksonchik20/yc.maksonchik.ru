import logging
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from ghost_note.telegram_bot import process_telegram_update
from ghost_note.telegram_notify import delete_telegram_webhook, get_telegram_updates, is_ghost_bot_configured

logger = logging.getLogger(__name__)

POLL_TIMEOUT = 25
ERROR_SLEEP = 5


class Command(BaseCommand):
    help = 'Long polling для Ghost Note Telegram-бота (обход проблем с webhook на Yandex Cloud)'

    def handle(self, *args, **options):
        if not is_ghost_bot_configured():
            self.stderr.write('GHOST_NOTE_BOT_TOKEN не задан')
            return

        delete_telegram_webhook(drop_pending_updates=True)
        self.stdout.write(self.style.SUCCESS('Webhook отключён, запуск long polling…'))

        offset = 0
        while True:
            try:
                close_old_connections()
                data = get_telegram_updates(offset=offset, poll_timeout=POLL_TIMEOUT)
                updates = data.get('result') or []
                for update in updates:
                    update_id = update.get('update_id')
                    if update_id is not None:
                        offset = update_id + 1
                    try:
                        close_old_connections()
                        process_telegram_update(update)
                    except Exception:
                        logger.exception('Failed to process update %s', update_id)
                    finally:
                        close_old_connections()
            except KeyboardInterrupt:
                self.stdout.write('Остановка…')
                return
            except Exception as exc:
                logger.exception('Telegram polling failed: %s', exc)
                time.sleep(ERROR_SLEEP)
