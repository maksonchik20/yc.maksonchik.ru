"""Opt-in text receipts; legacy polling remains consume-on-read."""

import json
import re
import uuid

from .models import GhostTextMessage


MAX_BATCH_MESSAGES = 50
INBOX_BYTE_BUDGET = 192 * 1024
MAX_MESSAGE_ID = 9223372036854775807


class TextDeliveryError(Exception):
    def __init__(self, code, status=400):
        self.code = code
        self.status = status
        super().__init__(code)


def store_viewer_text(session, text, idempotency_key=None):
    if idempotency_key is None:
        # Existing Windows/macOS viewers may omit this field entirely.
        return GhostTextMessage.objects.create(session=session, text=text)
    if not isinstance(idempotency_key, str):
        raise TextDeliveryError('invalid_idempotency_key')
    try:
        event_id = uuid.UUID(idempotency_key)
    except (ValueError, AttributeError):
        raise TextDeliveryError('invalid_idempotency_key')
    if str(event_id) != idempotency_key.lower():
        raise TextDeliveryError('invalid_idempotency_key')
    if len(text.encode('utf-8')) > 65536:
        raise TextDeliveryError('text_too_large', 413)
    # The existing (session, event_id) UNIQUE constraint handles races and
    # retries after a committed INSERT whose HTTP response was lost.
    message, _created = GhostTextMessage.objects.get_or_create(
        session=session, event_id=event_id, defaults={'text': text, 'source': 'viewer'})
    if message.text != text or message.source != 'viewer':
        raise TextDeliveryError('idempotency_conflict', 409)
    return message


def inbox_messages(session):
    messages = []
    size = 64
    pending = GhostTextMessage.objects.filter(session=session, delivered=False).order_by('created_at', 'pk')[:MAX_BATCH_MESSAGES]
    for message in pending:
        fieldToValue = {'id': str(message.pk), 'text': message.text}
        encoded_size = len(json.dumps(fieldToValue, ensure_ascii=False).encode('utf-8')) + 2
        # Keep at least one item, including pre-existing oversized legacy text.
        # Merely offering a message must never consume it, even on a retry.
        if messages and size + encoded_size > INBOX_BYTE_BUDGET:
            break
        messages.append(fieldToValue)
        size += encoded_size
    return messages


def acknowledge_messages(session, message_ids):
    if not isinstance(message_ids, list) or not 1 <= len(message_ids) <= MAX_BATCH_MESSAGES:
        raise TextDeliveryError('invalid_message_ids')
    if any(not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,18}', value)
           or int(value) > MAX_MESSAGE_ID for value in message_ids):
        raise TextDeliveryError('invalid_message_ids')
    # Scope every lookup/update to the authenticated producer session. Duplicate
    # receipts are harmless, including when the previous ACK response was lost.
    ids = {int(value) for value in message_ids}
    existing = GhostTextMessage.objects.filter(session=session, pk__in=ids)
    if existing.count() != len(ids):
        raise TextDeliveryError('unknown_message_ids', 400)
    existing.filter(delivered=False).update(delivered=True)
