import json
import logging
import signal
import threading
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import DatabaseError, InterfaceError, close_old_connections, connection
from django.utils import timezone

from ghost_note.outbox import next_outbox_attempt, process_outbox
from ghost_note.outbox_wake import read_wake_version, wait_for_outbox_work


logger = logging.getLogger(__name__)


def _close_connections_safely(close_connections):
    previous_connection = connection.connection
    try:
        close_connections()
    except (DatabaseError, InterfaceError) as exc:
        # mysqlclient can raise "server has gone away" when closing an already
        # disconnected native handle. Django clears its wrapper in finally;
        # only then is opening a fresh connection in this batch safe.
        code = exc.args[0] if exc.args else None
        if (previous_connection is None or connection.connection is not None
                or code not in (0, 2006, 2013, 2055)):
            raise


class Command(BaseCommand):
    help = 'Повторная отправка сохранённых сообщений Ghost Note'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=50)
        parser.add_argument('--loop', action='store_true')
        parser.add_argument('--poll-interval', type=float, default=1.0,
                            help='Local wake marker check interval (at most 1 second)')
        parser.add_argument('--idle-interval', type=float, default=30.0,
                            help='Maximum interval between safety DB scans')

    def handle(self, *args, **optionToValue):
        limit = max(1, optionToValue['limit'])
        if not optionToValue['loop']:
            self.stdout.write(json.dumps(process_outbox(limit=limit)))
            return

        stop_requested = threading.Event()
        poll_interval = min(1.0, max(0.1, optionToValue['poll_interval']))
        idle_interval = min(30.0, max(poll_interval, optionToValue['idle_interval']))
        signalToHandler = {}
        old_connection_max_age = connection.settings_dict.get('CONN_MAX_AGE', 0)
        # Limit reuse to this dedicated worker. HTTP requests and the command
        # polling process retain their existing connection lifecycle.
        connection.settings_dict['CONN_MAX_AGE'] = 60

        def request_stop(signum, frame):
            stop_requested.set()

        try:
            for signum in (signal.SIGTERM, signal.SIGINT):
                signalToHandler[signum] = signal.signal(signum, request_stop)
            while not stop_requested.is_set():
                # Snapshot before querying: a commit/wakeup during either query
                # is still detected, instead of being accidentally acknowledged.
                observed_version = read_wake_version()
                try:
                    _close_connections_safely(close_old_connections)
                    # Django 3.2 has no CONN_HEALTH_CHECKS. A persistent MySQL
                    # connection may have been closed by the server while idle.
                    if connection.connection is not None and not connection.is_usable():
                        _close_connections_safely(connection.close)
                    metricToCount = process_outbox(limit=limit)
                    next_attempt_at = next_outbox_attempt()
                    if any(metricToCount.values()):
                        self.stdout.write(json.dumps(metricToCount))
                        self.stdout.flush()
                except Exception:
                    logger.exception('Ghost outbox batch failed; will retry')
                    next_attempt_at = timezone.now() + timedelta(seconds=poll_interval)
                finally:
                    try:
                        _close_connections_safely(close_old_connections)
                    except Exception:
                        logger.exception('Could not close stale Ghost outbox connections')
                wait_for_outbox_work(stop_requested, observed_version, next_attempt_at,
                                     poll_interval=poll_interval, idle_interval=idle_interval)
        finally:
            try:
                _close_connections_safely(connection.close)
            finally:
                connection.settings_dict['CONN_MAX_AGE'] = old_connection_max_age
                for signum, handler in signalToHandler.items():
                    signal.signal(signum, handler)
