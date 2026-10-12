"""Durable Telegram delivery; retries only the saved payload, never bot commands."""
import hashlib
import html
import json
import logging
import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta

from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from .models import GhostPurchaseOrder, GhostTelegramBotMessage, GhostTelegramOutbox
from .telegram_text import split_message_text

logger = logging.getLogger(__name__)
LEASE_SECONDS = 180
ALERT_AFTER_ATTEMPTS = 6
_update_id = ContextVar('ghost_telegram_update_id', default=None)


class TelegramAPIError(RuntimeError):
    def __init__(self, description, code=0, retry_after=0):
        super().__init__(description)
        self.code = code
        self.retry_after = retry_after


def safe_error(error):
    from django.conf import settings
    text = str(error)
    token = getattr(settings, 'GHOST_NOTE_BOT_TOKEN', '')
    if token:
        text = text.replace(token, '[REDACTED]')
    return re.sub(r'bot\d+:[A-Za-z0-9_-]+', 'bot[REDACTED]', text)[:1000]


@contextmanager
def telegram_update_scope(update_id):
    token = _update_id.set(update_id)
    try:
        yield
    finally:
        _update_id.reset(token)


def enqueue(*, chat_id, method, payload, idempotency_key=None, completion=None,
            is_alert=False):
    if not chat_id or method not in ('sendMessage', 'sendDocument'):
        raise ValueError('Invalid outbox recipient or method')
    update_id = _update_id.get()
    if idempotency_key is None and update_id is not None:
        digest = hashlib.sha256(json.dumps(
            [chat_id, method, payload], sort_keys=True, ensure_ascii=False,
        ).encode()).hexdigest()
        idempotency_key = 'update:{}:{}'.format(update_id, digest)
    defaults = dict(chat_id=chat_id, method=method, payload=payload,
                    completion=completion or {}, is_alert=is_alert)
    if idempotency_key:
        item, _ = GhostTelegramOutbox.objects.get_or_create(
            idempotency_key=idempotency_key, defaults=defaults,
        )
    else:
        item = GhostTelegramOutbox.objects.create(**defaults)
    return item


def enqueue_and_deliver(**kwargs):
    item = enqueue(**kwargs)
    # Never send a token or a purchase notification before its transaction commits.
    transaction.on_commit(lambda: _try_delivery(item.pk))
    item.refresh_from_db()
    return {'ok': item.status != item.Status.FAILED, 'queued': item.status == item.Status.PENDING,
            'status': item.status, 'outbox_id': item.pk,
            'log_message_id': item.log_message_id}


def _try_delivery(pk):
    try:
        deliver(pk)
    except Exception:
        # The committed row and its lease make a worker retry safe after a DB failure.
        logger.exception('Ghost outbox worker error id=%s', pk)


def _claim(pk, now):
    with transaction.atomic():
        item = GhostTelegramOutbox.objects.select_for_update().filter(pk=pk).first()
        if not item or item.status != item.Status.PENDING or item.next_attempt_at > now:
            return None
        # Keep text, keyboard and installer in order even across worker processes.
        if GhostTelegramOutbox.objects.filter(
            chat_id=item.chat_id, status=item.Status.PENDING, pk__lt=item.pk,
        ).exists():
            return None
        item.lease_token = uuid.uuid4().hex
        item.next_attempt_at = now + timedelta(seconds=LEASE_SECONDS)
        item.attempts += 1
        item.save(update_fields=['lease_token', 'next_attempt_at', 'attempts'])
        return item


def _owned(item):
    return GhostTelegramOutbox.objects.filter(
        pk=item.pk, status=item.Status.PENDING, lease_token=item.lease_token,
    )


def _dispatch(item):
    from .telegram_notify import _call_telegram_api
    payload = dict(item.payload)
    if item.method == 'sendDocument':
        if not item.next_part:
            _call_telegram_api(item.method, payload, timeout=5)
            if not _owned(item).update(next_part=1):
                raise RuntimeError('Outbox lease lost')
        return
    parts = split_message_text(payload['text'], payload.get('parse_mode'))
    for index in range(item.next_part, len(parts)):
        if not _owned(item).update(next_attempt_at=timezone.now() + timedelta(seconds=LEASE_SECONDS)):
            raise RuntimeError('Outbox lease lost')
        part_payload = dict(payload)
        part_payload['text'] = (parts[index] if len(parts) == 1 else
                                'Часть {}/{}\n\n{}'.format(index + 1, len(parts), parts[index]))
        if index < len(parts) - 1:
            part_payload.pop('reply_markup', None)
        _call_telegram_api(item.method, part_payload, timeout=5)
        if not _owned(item).update(next_part=index + 1):
            raise RuntimeError('Outbox lease lost')


def _complete_order(item, error=None):
    for key, prefix in [('purchase_order', 'telegram'), ('referrer_order', 'referrer')]:
        order_id = item.completion.get(key)
        if order_id:
            values = {prefix + '_notify_error': error or ''}
            if error is None:
                values[prefix + '_notified_at'] = timezone.now()
            GhostPurchaseOrder.objects.filter(pk=order_id).update(**values)


def _record_success(item):
    from .bot_logging import contact_meta_for_chat_id
    with transaction.atomic():
        current = _owned(item).select_for_update().first()
        if not current:
            return 'skipped'
        username, first_name = contact_meta_for_chat_id(item.chat_id)
        is_document = item.method == 'sendDocument'
        log = GhostTelegramBotMessage.objects.create(
            direction=GhostTelegramBotMessage.Direction.OUT,
            telegram_user_id=item.chat_id, username=username, first_name=first_name,
            text=item.payload.get('caption', '[document]') if is_document else item.payload['text'],
            message_kind=(GhostTelegramBotMessage.MessageKind.DOCUMENT if is_document else
                          GhostTelegramBotMessage.MessageKind.TEXT),
        )
        current.status = current.Status.SENT
        current.sent_at = timezone.now()
        current.last_error = ''
        current.log_message = log
        current.save(update_fields=['status', 'sent_at', 'last_error', 'log_message'])
        _complete_order(item)
    return 'sent'


def _permanent(exc):
    if not isinstance(exc, TelegramAPIError):
        return isinstance(exc, (ValueError, KeyError))
    if 'temporarily unavailable' in str(exc).lower():
        return False
    return exc.code in (400, 403, 404)


def _record_failure(item, exc):
    from .telegram_notify import _admin_chat_id
    error = safe_error(exc)
    permanent = _permanent(exc)
    delay = min(5 * (2 ** min(item.attempts - 1, 10)), 3600)
    delay = max(delay, min(int(getattr(exc, 'retry_after', 0) or 0), 86400))
    with transaction.atomic():
        current = _owned(item).select_for_update().first()
        if not current:
            return 'skipped'
        current.status = current.Status.FAILED if permanent else current.Status.PENDING
        current.last_error = error
        current.next_attempt_at = timezone.now() + timedelta(seconds=delay)
        current.save(update_fields=['status', 'last_error', 'next_attempt_at'])
        _complete_order(item, error=error[:500])
        if (permanent or item.attempts >= ALERT_AFTER_ATTEMPTS) and not current.alerted_at and not item.is_alert:
            owner = _admin_chat_id()
            if owner:
                enqueue(chat_id=owner, method='sendMessage', is_alert=True,
                        idempotency_key='outbox-alert:{}'.format(item.pk), payload={
                            'chat_id': owner, 'parse_mode': 'HTML',
                            'text': ('⚠️ <b>Ghost Note — ошибка отправки</b>\n\n'
                                     'ID: {}\nМетод: {}\nChat ID: {}\nПопыток: {}\n'
                                     'Ошибка: {}\n\n{}').format(
                                         item.pk, item.method, item.chat_id, item.attempts,
                                         html.escape(error[:500]),
                                         'Повторные попытки остановлены.' if permanent else
                                         'Сообщение сохранено, повторные попытки продолжаются.'),
                        })
                current.alerted_at = timezone.now()
                current.save(update_fields=['alerted_at'])
    logger.warning('Ghost outbox %s id=%s attempt=%s error=%s',
                   'failed' if permanent else 'retry', item.pk, item.attempts, error)
    return 'failed' if permanent else 'retry'


def deliver(pk, now=None):
    item = _claim(pk, now or timezone.now())
    if not item:
        return 'skipped'
    try:
        _dispatch(item)
    except Exception as exc:
        return _record_failure(item, exc)
    return _record_success(item)


def process_outbox(limit=50):
    stats = {'sent': 0, 'retry': 0, 'failed': 0, 'skipped': 0}
    # Re-evaluate after each send, allowing the next message in that chat to proceed.
    visited = []
    earlier = GhostTelegramOutbox.objects.filter(
        chat_id=OuterRef('chat_id'), status=GhostTelegramOutbox.Status.PENDING,
        pk__lt=OuterRef('pk'),
    )
    for _ in range(limit):
        item = (GhostTelegramOutbox.objects.filter(
            status=GhostTelegramOutbox.Status.PENDING, next_attempt_at__lte=timezone.now(),
        ).exclude(pk__in=visited).annotate(has_earlier=Exists(earlier))
                .filter(has_earlier=False).order_by('pk').first())
        if not item:
            break
        visited.append(item.pk)
        stats[deliver(item.pk)] += 1
    return stats
