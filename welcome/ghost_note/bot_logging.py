import html
import logging
import re

from .models import GhostTelegramBotMessage, GhostTelegramContact

logger = logging.getLogger(__name__)

_HTML_TAG_RE = re.compile(r'<[^>]+>')


def strip_html(value):
    return _HTML_TAG_RE.sub('', value or '').strip()


def normalize_username(username):
    return (username or '').strip().lstrip('@')


def contact_meta_for_chat_id(chat_id):
    if not chat_id:
        return '', ''
    try:
        contact = GhostTelegramContact.objects.filter(telegram_user_id=chat_id).first()
    except Exception:
        logger.exception('Failed to load Telegram contact for chat_id=%s', chat_id)
        return '', ''
    if not contact:
        return '', ''
    return normalize_username(contact.username), ''


def log_bot_message(
    *,
    direction,
    telegram_user_id,
    text='',
    username='',
    first_name='',
    message_kind=GhostTelegramBotMessage.MessageKind.TEXT,
):
    if not telegram_user_id:
        return None

    normalized_username = normalize_username(username)
    if not normalized_username:
        normalized_username, _ = contact_meta_for_chat_id(telegram_user_id)

    try:
        return GhostTelegramBotMessage.objects.create(
            direction=direction,
            telegram_user_id=telegram_user_id,
            username=normalized_username,
            first_name=(first_name or '').strip(),
            message_kind=message_kind,
            text=(text or '').strip(),
        )
    except Exception:
        logger.exception('Failed to log bot message for chat_id=%s', telegram_user_id)
        return None


def log_incoming_update(payload):
    callback_query = payload.get('callback_query')
    if callback_query:
        from_user = callback_query.get('from') or {}
        chat = (callback_query.get('message') or {}).get('chat') or {}
        chat_id = chat.get('id') or from_user.get('id')
        data = (callback_query.get('data') or '').strip()
        log_bot_message(
            direction=GhostTelegramBotMessage.Direction.IN,
            telegram_user_id=chat_id,
            username=from_user.get('username', ''),
            first_name=from_user.get('first_name', ''),
            message_kind=GhostTelegramBotMessage.MessageKind.CALLBACK,
            text=f'[callback] {data}' if data else '[callback]',
        )
        return

    message = payload.get('message') or payload.get('edited_message')
    if not message:
        return

    chat = message.get('chat') or {}
    from_user = message.get('from') or {}
    chat_id = chat.get('id')
    username = (chat.get('username') or from_user.get('username') or '').strip()
    first_name = (from_user.get('first_name') or '').strip()
    prefix = '[edited] ' if payload.get('edited_message') else ''

    if message.get('text'):
        log_bot_message(
            direction=GhostTelegramBotMessage.Direction.IN,
            telegram_user_id=chat_id,
            username=username,
            first_name=first_name,
            text=f'{prefix}{message.get("text", "").strip()}',
        )
        return

    if message.get('document'):
        doc = message['document']
        file_name = (doc.get('file_name') or 'document').strip()
        log_bot_message(
            direction=GhostTelegramBotMessage.Direction.IN,
            telegram_user_id=chat_id,
            username=username,
            first_name=first_name,
            message_kind=GhostTelegramBotMessage.MessageKind.DOCUMENT,
            text=f'{prefix}[document] {file_name}',
        )
        return

    for key in ('photo', 'video', 'voice', 'audio', 'sticker', 'contact', 'location'):
        if message.get(key):
            log_bot_message(
                direction=GhostTelegramBotMessage.Direction.IN,
                telegram_user_id=chat_id,
                username=username,
                first_name=first_name,
                message_kind=GhostTelegramBotMessage.MessageKind.OTHER,
                text=f'{prefix}[{key}]',
            )
            return

    log_bot_message(
        direction=GhostTelegramBotMessage.Direction.IN,
        telegram_user_id=chat_id,
        username=username,
        first_name=first_name,
        message_kind=GhostTelegramBotMessage.MessageKind.OTHER,
        text=f'{prefix}[message]',
    )


def log_outgoing_message(*, chat_id, text='', message_kind=GhostTelegramBotMessage.MessageKind.TEXT):
    log_bot_message(
        direction=GhostTelegramBotMessage.Direction.OUT,
        telegram_user_id=chat_id,
        text=text,
        message_kind=message_kind,
    )


def format_chat_message_html(message):
    plain = strip_html(message.text)
    safe = html.escape(plain).replace('\n', '<br>')
    css_class = 'ghost-chat-out' if message.direction == GhostTelegramBotMessage.Direction.OUT else 'ghost-chat-in'
    who = f'@{message.username}' if message.username else str(message.telegram_user_id)
    when = message.created_at.strftime('%d.%m.%Y %H:%M:%S')
    label = 'Бот' if message.direction == GhostTelegramBotMessage.Direction.OUT else who
    return (
        f'<div class="ghost-chat-row {css_class}">'
        f'<div class="ghost-chat-meta">{html.escape(label)} · {when}</div>'
        f'<div class="ghost-chat-text">{safe}</div>'
        f'</div>'
    )


def resolve_chat_id(*, username='', telegram_user_id=''):
    if telegram_user_id:
        try:
            return int(telegram_user_id)
        except (TypeError, ValueError):
            return None

    normalized = normalize_username(username)
    if not normalized:
        return None

    contact = GhostTelegramContact.objects.filter(username__iexact=normalized).first()
    if contact:
        return contact.telegram_user_id

    message = (
        GhostTelegramBotMessage.objects.filter(username__iexact=normalized)
        .order_by('-created_at')
        .first()
    )
    if message:
        return message.telegram_user_id

    return None
