import json
import logging
import signal
import threading

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from ghost_note.outbox import process_outbox


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Повторная отправка сохранённых сообщений Ghost Note'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=50)
        parser.add_argument('--loop', action='store_true')
        parser.add_argument('--poll-interval', type=float, default=2.0)

    def handle(self, *args, **optionToValue):
        limit = max(1, optionToValue['limit'])
        if not optionToValue['loop']:
            self.stdout.write(json.dumps(process_outbox(limit=limit)))
            return

        stop_requested = threading.Event()
        poll_interval = max(0.1, optionToValue['poll_interval'])
        signalToHandler = {}

        def request_stop(signum, frame):
            stop_requested.set()

        try:
            for signum in (signal.SIGTERM, signal.SIGINT):
                signalToHandler[signum] = signal.signal(signum, request_stop)
            while not stop_requested.is_set():
                try:
                    close_old_connections()
                    metricToCount = process_outbox(limit=limit)
                    if any(metricToCount.values()):
                        self.stdout.write(json.dumps(metricToCount))
                        self.stdout.flush()
                except Exception:
                    logger.exception('Ghost outbox batch failed; will retry')
                finally:
                    try:
                        close_old_connections()
                    except Exception:
                        logger.exception('Could not close stale Ghost outbox connections')
                stop_requested.wait(poll_interval)
        finally:
            for signum, handler in signalToHandler.items():
                signal.signal(signum, handler)
