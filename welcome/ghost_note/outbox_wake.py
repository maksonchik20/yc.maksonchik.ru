"""Best-effort local wakeups; the database remains the durable source of work."""
import logging
import os
import tempfile
import time

from django.conf import settings
from django.utils import timezone


logger = logging.getLogger(__name__)


def _wake_path():
    return getattr(settings, 'GHOST_OUTBOX_WAKE_PATH', '/run/ghost-outbox/wake')


def notify_outbox_worker():
    # RuntimeDirectory is private to www-data. Atomic replacement does not lose
    # concurrent notifications or require a running worker/socket receiver.
    path = _wake_path()
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix='.wake-', dir=os.path.dirname(path))
        os.close(descriptor)
        os.replace(temporary, path)
    except OSError:
        # Delivery must not fail just because the worker is stopped. It always
        # scans on startup and periodically checks the DB if a wakeup is lost.
        logger.debug('Ghost outbox wake unavailable; periodic scan will recover', exc_info=True)
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def read_wake_version():
    try:
        info = os.stat(_wake_path(), follow_symlinks=False)
        return info.st_ino, info.st_mtime_ns, info.st_ctime_ns
    except OSError:
        return None


def wait_for_outbox_work(stop_requested, observed_version, next_attempt_at,
                         poll_interval=1.0, idle_interval=30.0):
    """Wait without SQL, bounded by a retry/lease deadline and a safety scan."""
    remaining = idle_interval
    if next_attempt_at is not None:
        remaining = min(remaining, max(0, (next_attempt_at - timezone.now()).total_seconds()))
    deadline = time.monotonic() + remaining
    while not stop_requested.is_set():
        if read_wake_version() != observed_version:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        stop_requested.wait(min(poll_interval, remaining))
