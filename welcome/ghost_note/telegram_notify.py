import html
import logging
import re
import threading
import time

import requests
from requests.adapters import HTTPAdapter
from django.conf import settings
from django.utils import timezone

from .outbox import TelegramAPIError, enqueue_and_deliver, safe_error
from .models import GhostPurchaseOrder, GhostTelegramContact
from .purchase_tips import DOWNLOAD_FILENAME, SUPPORT_TELEGRAM, SUPPORT_TELEGRAM_URL, post_purchase_telegram_html
from .referrals import calculate_commission

logger = logging.getLogger(__name__)

TELEGRAM_USERNAME_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]{4,31}$')

# Как в WhoUpdate: основной Bot API и два резервных адреса.
TELEGRAM_API_ENDPOINTS = (
    {'base': 'https://api.telegram.org', 'host': None, 'verify': True, 'connect_timeout': 2},
    {'base': 'https://149.154.167.220', 'host': 'api.telegram.org', 'verify': False, 'connect_timeout': 0.75},
    {'base': 'https://149.154.167.99', 'host': 'api.telegram.org', 'verify': False, 'connect_timeout': 0.75},
)
TELEGRAM_API_TIMEOUT = 8
TELEGRAM_CONNECT_TIMEOUT = 2
TELEGRAM_CIRCUIT_COOLDOWN = 30

TELEGRAM_SESSION = requests.Session()
TELEGRAM_SESSION.mount('https://', HTTPAdapter(pool_connections=10, pool_maxsize=20))

_endpointBaseToRetryAt = {}
_endpoint_failures_lock = threading.Lock()


def _endpoint_is_available(base, now):
    with _endpoint_failures_lock:
        return _endpointBaseToRetryAt.get(base, 0) <= now


def _mark_endpoint_failed(base):
    with _endpoint_failures_lock:
        _endpointBaseToRetryAt[base] = time.monotonic() + TELEGRAM_CIRCUIT_COOLDOWN


def _mark_endpoint_healthy(base):
    with _endpoint_failures_lock:
        _endpointBaseToRetryAt.pop(base, None)


def is_ghost_bot_configured():
    return bool(getattr(settings, 'GHOST_NOTE_BOT_TOKEN', ''))


def normalize_telegram_input(value):
    raw = (value or '').strip()
    if not raw:
        return ''
    if raw.startswith('https://t.me/'):
        raw = raw.rsplit('/', 1)[-1]
    if raw.startswith('@'):
        raw = raw[1:]
    return raw.strip()


def parse_telegram_recipient(value):
    """
    Возвращает chat_id (int) для sendMessage или None, если адресат не определён.
    Числовой ID — сразу. @username — только если пользователь уже писал боту /start.
    """
    normalized = normalize_telegram_input(value)
    if not normalized:
        return None
    if normalized.lstrip('-').isdigit():
        return int(normalized)
    if TELEGRAM_USERNAME_RE.match(normalized):
        contact = GhostTelegramContact.objects.filter(
            username__iexact=normalized,
        ).order_by('-updated_at').first()
        if contact:
            return contact.telegram_user_id
    return None


def referrer_chat_id(referrer):
    if not referrer:
        return None
    username = (referrer.telegram_username or '').strip()
    if username:
        chat_id = parse_telegram_recipient(username)
        if chat_id:
            return chat_id
    return None


def format_referrer_purchase_message(order, *, commission_amount):
    return (
        '🎉 <b>Покупка по вашей реферальной ссылке</b>\n\n'
        f'Покупатель: {html.escape(order.customer_name)}\n'
        f'Сумма оплаты: {order.amount} ₽\n'
        f'Ваша комиссия (20%): <b>{commission_amount:.2f} ₽</b>\n\n'
        f'За выплатой обращайтесь к '
        f'<a href="{SUPPORT_TELEGRAM_URL}">{SUPPORT_TELEGRAM}</a>.'
    )


def notify_referrer_purchase(order):
    """
    Уведомляет пригласившего о покупке по его реферальному ключу.
    Повторно не шлёт, если уже отправлено.
    """
    order = GhostPurchaseOrder.objects.select_related('referrer').get(pk=order.pk)

    if order.referrer_notified_at:
        return True

    if not order.referrer_id:
        return False

    if not is_ghost_bot_configured():
        order.referrer_notify_error = 'Бот не настроен (GHOST_NOTE_BOT_TOKEN)'
        order.save(update_fields=['referrer_notify_error'])
        return False

    chat_id = referrer_chat_id(order.referrer)
    if not chat_id:
        order.referrer_notify_error = (
            'Не удалось определить Telegram пригласившего. '
            'Нужно написать боту /start.'
        )
        order.save(update_fields=['referrer_notify_error'])
        return False

    commission_amount = calculate_commission(order.amount)
    try:
        send_bot_message(
            chat_id=chat_id,
            text=format_referrer_purchase_message(order, commission_amount=commission_amount),
            idempotency_key='referrer-order:{}'.format(order.pk),
            completion={'referrer_order': order.pk},
        )
    except Exception as exc:
        logger.exception('Referrer notify failed for order %s', order.public_id)
        order.referrer_notify_error = safe_error(exc)[:500]
        order.save(update_fields=['referrer_notify_error'])
        return False

    # The outbox records notified_at only after Telegram confirms delivery.
    return True


def _admin_chat_id():
    value = getattr(settings, 'GHOST_NOTE_ADMIN_CHAT_ID', '') or getattr(settings, 'OWNER_CHAT_ID', '')
    if not value:
        return None
    return int(value)


def _format_order_brief(order):
    starts_at = timezone.localtime(order.starts_at)
    lines = [
        f'Имя: {html.escape(order.customer_name)}',
        f'Telegram: {html.escape(order.customer_telegram or "—")}',
        f'E-mail: {html.escape(order.customer_email or "—")}',
        f'Тариф: {html.escape(order.get_access_type_display())}',
        f'Сумма: {order.amount} ₽',
        f'Длительность: {order.duration_minutes} мин',
        f'Начало: {starts_at.strftime("%d.%m.%Y %H:%M")} МСК',
        f'Заказ: <code>{order.public_id}</code>',
    ]
    if order.referral_key_input:
        lines.append(f'Реф. ключ: {html.escape(order.referral_key_input)}')
    if order.yookassa_payment_id:
        lines.append(f'Платёж: <code>{html.escape(order.yookassa_payment_id)}</code>')
    if order.token_id:
        lines.append(f'Токен: <code>{html.escape(order.token.token)}</code>')
    lines.append(f'Статус: {html.escape(order.get_status_display())}')
    return '\n'.join(lines)


def notify_admin(text):
    chat_id = _admin_chat_id()
    if not chat_id or not is_ghost_bot_configured():
        return
    try:
        send_bot_message(chat_id=chat_id, text=text)
    except Exception:
        logger.exception('Admin telegram notify failed')


def notify_admin_new_bot_user(*, chat_id, username='', first_name=''):
    username_display = f'@{username}' if username else '—'
    name = html.escape((first_name or '').strip() or '—')
    notify_admin(
        '👤 <b>Ghost Note — новый пользователь бота</b>\n\n'
        f'Username: {html.escape(username_display)}\n'
        f'Имя: {name}\n'
        f'ID: <code>{chat_id}</code>'
    )


def _test_token_source(token):
    user = token.user
    if not user:
        return 'Неизвестно'

    username = (user.telegram_username or '').strip().lstrip('@')
    if username:
        contact = GhostTelegramContact.objects.filter(username__iexact=username).first()
        if contact and contact.last_trial_at and token.starts_at:
            delta = abs((contact.last_trial_at - token.starts_at).total_seconds())
            if delta < 10:
                return 'Telegram /trial'

    if user.name.upper().startswith('TG '):
        return 'Telegram /trial'

    return 'Админка'


def notify_admin_test_token(token):
    from .auth import format_token_datetime
    from .models import GhostAccessToken

    token = GhostAccessToken.objects.select_related('user').get(pk=token.pk)
    user = token.user
    user_name = html.escape(user.name if user else '—')

    tg_username = (user.telegram_username or '').strip().lstrip('@') if user else ''
    if tg_username:
        tg_display = html.escape(f'@{tg_username}')
        tg_line = (
            f'Telegram: <a href="https://t.me/{html.escape(tg_username)}">{tg_display}</a>'
        )
    else:
        tg_line = 'Telegram: —'

    modes = []
    if token.allow_local:
        modes.append('локальный')
    if token.allow_remote:
        modes.append('удалённый')

    notify_admin(
        '🧪 <b>Ghost Note — создан тестовый токен</b>\n\n'
        f'Источник: {html.escape(_test_token_source(token))}\n'
        f'Пользователь: {user_name}\n'
        f'{tg_line}\n'
        f'Токен: <code>{html.escape(token.token)}</code>\n'
        f'Режим: {html.escape(", ".join(modes) or "—")}\n'
        f'С {format_token_datetime(token.starts_at)} '
        f'до {format_token_datetime(token.expires_at)}'
    )


def notify_admin_operator_called(*, chat_id, username='', first_name=''):
    from urllib.parse import quote

    username_display = f'@{username}' if username else '—'
    name = html.escape((first_name or '').strip() or '—')
    if username:
        chat_url = (
            'https://yc.maksonchik.ru/admin/ghost_note/ghosttelegrambotmessage/'
            f'?username={quote(username)}'
        )
        user_line = (
            f'Пользователь: <a href="{chat_url}">{html.escape(username_display)}</a>'
        )
    else:
        user_line = f'Пользователь: {html.escape(username_display)}'
    notify_admin(
        '🆘 <b>Ghost Note — вызов оператора</b>\n\n'
        f'{user_line} позвал оператора.\n'
        f'Имя: {name}\n'
        f'ID: <code>{chat_id}</code>'
    )


def notify_admin_purchase_attempt(order, *, note=''):
    notify_admin(
        '🛒 <b>Ghost Note — попытка покупки</b>\n\n'
        f'{_format_order_brief(order)}'
        + (f'\n\n{html.escape(note)}' if note else '')
    )


def notify_admin_purchase_paid(order, *, token_value):
    order = GhostPurchaseOrder.objects.select_related('token').get(pk=order.pk)
    notify_admin(
        '✅ <b>Ghost Note — оплата получена</b>\n\n'
        f'{_format_order_brief(order)}'
    )


def notify_admin_purchase_failed(order, *, reason):
    notify_admin(
        '❌ <b>Ghost Note — покупка не удалась</b>\n\n'
        f'{_format_order_brief(order)}\n\n'
        f'Причина: {html.escape(reason)}'
    )


def notify_admin_form_attempt(*, customer_name='', customer_telegram='', customer_email='', errors):
    name = html.escape(customer_name or '—')
    tg = html.escape(customer_telegram or '—')
    email = html.escape(customer_email or '—')
    error_text = html.escape('; '.join(errors) or 'ошибка формы')
    notify_admin(
        '⚠️ <b>Ghost Note — незавершённая попытка</b>\n\n'
        f'Имя: {name}\n'
        f'Telegram: {tg}\n'
        f'E-mail: {email}\n'
        f'Ошибка: {error_text}'
    )


def send_installer_document(*, chat_id, idempotency_key=None):
    installer_file_id = getattr(settings, 'GHOST_NOTE_INSTALLER_FILE_ID', '')
    if not installer_file_id:
        return
    send_bot_document(
        chat_id=chat_id,
        file_id=installer_file_id,
        caption=f'📥 {DOWNLOAD_FILENAME} — программа Ghost Note',
        idempotency_key=idempotency_key,
    )


def notify_trial_token(*, chat_id, token):
    from .trial import trial_issued_text

    return send_bot_message(chat_id=chat_id, text=trial_issued_text(token),
                            idempotency_key='trial-token:{}'.format(token.pk))


def format_purchase_message(order, token_value):
    starts_at = timezone.localtime(order.starts_at)
    lines = [
        '✅ <b>Ghost Note</b> — оплата получена',
        '',
        f'Токен: <code>{html.escape(token_value)}</code>',
        f'Режим: {html.escape(order.get_access_type_display())}',
        f'Начало: {starts_at.strftime("%d.%m.%Y %H:%M")} МСК',
        f'Длительность: {order.duration_minutes} мин',
        '',
        'Сохраните токен — он нужен для входа в программу.',
        'Обычно токен выдаётся сразу после оплаты.',
        '',
        post_purchase_telegram_html(),
    ]
    return '\n'.join(lines)


def _call_telegram_api(method, parameterToValue, timeout=None):
    token = getattr(settings, 'GHOST_NOTE_BOT_TOKEN', '')
    if not token:
        raise RuntimeError('GHOST_NOTE_BOT_TOKEN не задан')

    if timeout is None:
        timeout = getattr(settings, 'TELEGRAM_API_TIMEOUT', TELEGRAM_API_TIMEOUT)

    endpoints = getattr(settings, 'TELEGRAM_API_ENDPOINTS', TELEGRAM_API_ENDPOINTS)
    last_error = None
    now = time.monotonic()
    for endpointKeyToValue in endpoints:
        base = endpointKeyToValue['base']
        if not _endpoint_is_available(base, now):
            continue
        url = f"{base.rstrip('/')}/bot{token}/{method}"
        headerToValue = {}
        host = endpointKeyToValue.get('host')
        if host:
            headerToValue['Host'] = host
        try:
            response = TELEGRAM_SESSION.post(
                url,
                json=parameterToValue,
                headers=headerToValue or None,
                timeout=(endpointKeyToValue.get('connect_timeout', TELEGRAM_CONNECT_TIMEOUT), timeout),
                verify=endpointKeyToValue.get('verify', True),
            )
        except requests.RequestException as exc:
            _mark_endpoint_failed(base)
            last_error = RuntimeError(safe_error(exc))
            logger.warning('Telegram API %s via %s failed: %s', method, base, last_error)
            continue

        _mark_endpoint_healthy(base)
        try:
            responseKeyToValue = response.json()
        except ValueError:
            raise RuntimeError('Telegram API returned invalid JSON (HTTP {})'.format(response.status_code)) from None
        if responseKeyToValue.get('ok'):
            return responseKeyToValue
        # A Telegram response (403, 429, etc.) is not a failed route. Let the
        # durable outbox apply permanent-error handling or retry_after once.
        raise TelegramAPIError(
            responseKeyToValue.get('description', 'Telegram API error'),
            code=responseKeyToValue.get('error_code', response.status_code),
            retry_after=(responseKeyToValue.get('parameters') or {}).get('retry_after', 0),
        )

    if last_error:
        raise last_error
    raise RuntimeError('Telegram API endpoints temporarily disabled by circuit breaker')


def send_bot_message(*, chat_id, text, timeout=10, reply_markup=None, parse_mode='HTML',
                     idempotency_key=None, completion=None):
    if not chat_id:
        raise ValueError('chat_id не указан')

    payload = {
        'chat_id': chat_id,
        'text': text,
        'disable_web_page_preview': True,
    }
    if parse_mode:
        payload['parse_mode'] = parse_mode
    if reply_markup:
        payload['reply_markup'] = reply_markup

    return enqueue_and_deliver(chat_id=chat_id, method='sendMessage', payload=payload,
                               idempotency_key=idempotency_key, completion=completion)


def answer_callback_query(*, callback_query_id, text='', show_alert=False, timeout=10):
    if not callback_query_id:
        raise ValueError('callback_query_id не указан')

    payload = {
        'callback_query_id': callback_query_id,
        'show_alert': show_alert,
    }
    if text:
        payload['text'] = text[:200]
    return _call_telegram_api('answerCallbackQuery', payload, timeout=timeout)


def delete_telegram_webhook(*, drop_pending_updates=False, timeout=10):
    payload = {}
    if drop_pending_updates:
        payload['drop_pending_updates'] = True
    return _call_telegram_api('deleteWebhook', payload, timeout=timeout)


def get_telegram_updates(*, offset=0, poll_timeout=25, timeout=None):
    if timeout is None:
        timeout = poll_timeout + 10
    return _call_telegram_api(
        'getUpdates',
        {
            'offset': offset,
            'timeout': poll_timeout,
            'allowed_updates': ['message', 'callback_query'],
        },
        timeout=timeout,
    )


def send_bot_document(*, chat_id, file_id, caption='', timeout=30, idempotency_key=None):
    if not chat_id:
        raise ValueError('chat_id не указан')
    if not file_id:
        raise ValueError('file_id не указан')

    payload = {
        'chat_id': chat_id,
        'document': file_id,
    }
    if caption:
        payload['caption'] = caption[:1024]
        payload['parse_mode'] = 'HTML'

    return enqueue_and_deliver(chat_id=chat_id, method='sendDocument', payload=payload,
                               idempotency_key=idempotency_key)


def notify_purchase_token(order, *, token_value=None):
    """
    Отправляет токен покупателю в Telegram. Повторно не шлёт, если уже отправлено.
    """
    order = GhostPurchaseOrder.objects.select_related('token').get(pk=order.pk)

    if order.telegram_notified_at:
        return True

    if not order.customer_telegram:
        return False

    if not is_ghost_bot_configured():
        order.telegram_notify_error = 'Бот не настроен (GHOST_NOTE_BOT_TOKEN)'
        order.save(update_fields=['telegram_notify_error'])
        return False

    token_value = token_value or (order.token.token if order.token_id else '')
    if not token_value:
        return False

    chat_id = parse_telegram_recipient(order.customer_telegram)
    if not chat_id:
        order.telegram_notify_error = (
            'Не удалось определить chat_id. Напишите боту /start или укажите числовой Telegram ID.'
        )
        order.save(update_fields=['telegram_notify_error'])
        return False

    try:
        send_bot_message(
            chat_id=chat_id,
            text=format_purchase_message(order, token_value),
            idempotency_key='purchase-order:{}'.format(order.pk),
            completion={'purchase_order': order.pk},
        )
        send_installer_document(chat_id=chat_id,
                                idempotency_key='purchase-installer:{}'.format(order.pk))
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        logger.exception('Telegram notify failed for order %s', order.public_id)
        order.telegram_notify_error = safe_error(exc)[:500]
        order.save(update_fields=['telegram_notify_error'])
        return False

    # The outbox records notified_at only after Telegram confirms delivery.
    return True
