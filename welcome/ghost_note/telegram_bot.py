import logging

from django.conf import settings

from .bot_logging import log_incoming_update
from .models import GhostTelegramContact
from .purchase_tips import SUPPORT_TELEGRAM, SUPPORT_TELEGRAM_URL
from .referral_bot import (
    get_or_create_referrer_user,
    invited_help_text,
    parse_referral_start_payload,
    referral_info_text,
    set_invited_by_key,
)
from .telegram_notify import (
    answer_callback_query,
    notify_admin_new_bot_user,
    notify_admin_operator_called,
    notify_trial_token,
    send_bot_message,
    send_installer_document,
)
from .tokens_bot import purchased_tokens_text
from .help_bot import (
    HELP_CALLBACK_NO,
    HELP_CALLBACK_YES,
    help_back_text,
    help_confirm_keyboard,
    help_confirm_text,
    help_operator_called_text,
)
from .trial import (
    TRIAL_CALLBACK_NO,
    TRIAL_CALLBACK_YES,
    TrialCooldownError,
    issue_trial_token,
    trial_available_at,
    trial_cancelled_text,
    trial_confirm_keyboard,
    trial_confirm_text,
    trial_cooldown_text,
)

SITE_URL = 'https://yc.maksonchik.ru/'

logger = logging.getLogger(__name__)


def _welcome_text(chat_id, username):
    username_line = f'@{username}' if username else 'не указан'
    return (
        '👋 Добро пожаловать в Ghost Note!\n\n'
        f'Ваш Telegram ID: <code>{chat_id}</code>\n'
        f'Username: {username_line}\n\n'
        'При покупке на сайте укажите этот ID или @username — '
        'токен обычно приходит сюда сразу после оплаты.\n\n'
        'Пробный доступ на 20 минут: /trial\n'
        'Реферальная программа: /referral\n'
        'Кто пригласил: /invited\n'
        'Купленные и тестовые токены: /tokens\n'
        'Позвать оператора: /help\n\n'
        f'Сайт: <a href="{SITE_URL}">{SITE_URL}</a>\n\n'
        f'По всем вопросам пишите сюда: '
        f'<a href="{SUPPORT_TELEGRAM_URL}">{SUPPORT_TELEGRAM}</a>'
    )


def _bot_command(text):
    if not text or not text.startswith('/'):
        return ''
    return text.split()[0].split('@')[0].lower()


def _upsert_contact(chat_id, username='', first_name=''):
    contact, created = GhostTelegramContact.objects.update_or_create(
        telegram_user_id=chat_id,
        defaults={'username': username},
    )
    if created:
        notify_admin_new_bot_user(chat_id=chat_id, username=username, first_name=first_name)
    return contact


def _command_args(text):
    parts = (text or '').split()
    if len(parts) <= 1:
        return ''
    return parts[1].strip()


def _handle_start(*, chat_id, username, first_name='', text):
    _upsert_contact(chat_id, username, first_name)
    referral_key = parse_referral_start_payload(text)
    if referral_key:
        send_bot_message(
            chat_id=chat_id,
            text=set_invited_by_key(
                chat_id=chat_id,
                username=username,
                referral_key=referral_key,
            ),
        )
    send_bot_message(chat_id=chat_id, text=_welcome_text(chat_id, username))


def _handle_invited_request(*, chat_id, username, first_name='', args):
    _upsert_contact(chat_id, username, first_name)
    if not args:
        send_bot_message(chat_id=chat_id, text=invited_help_text())
        return
    send_bot_message(
        chat_id=chat_id,
        text=set_invited_by_key(chat_id=chat_id, username=username, referral_key=args),
    )


def _handle_tokens_request(*, chat_id, username, first_name=''):
    _upsert_contact(chat_id, username, first_name)
    send_bot_message(
        chat_id=chat_id,
        text=purchased_tokens_text(chat_id=chat_id, username=username),
    )


def _handle_referral_request(*, chat_id, username, first_name=''):
    _upsert_contact(chat_id, username, first_name)
    user = get_or_create_referrer_user(chat_id=chat_id, username=username)
    send_bot_message(chat_id=chat_id, text=referral_info_text(user))


def _handle_trial_request(*, chat_id, username, first_name=''):
    contact = _upsert_contact(chat_id, username, first_name)
    available_at = trial_available_at(contact)
    if available_at:
        send_bot_message(chat_id=chat_id, text=trial_cooldown_text(available_at))
        return

    send_bot_message(
        chat_id=chat_id,
        text=trial_confirm_text(),
        reply_markup=trial_confirm_keyboard(),
    )
    send_installer_document(chat_id=chat_id)


def _handle_help_request(*, chat_id, username, first_name=''):
    _upsert_contact(chat_id, username, first_name)
    send_bot_message(
        chat_id=chat_id,
        text=help_confirm_text(),
        reply_markup=help_confirm_keyboard(),
    )


def _process_help_callback(callback_query):
    callback_id = callback_query.get('id')
    data = (callback_query.get('data') or '').strip()
    from_user = callback_query.get('from') or {}
    chat = (callback_query.get('message') or {}).get('chat') or {}
    chat_id = chat.get('id')
    username = (from_user.get('username') or '').strip()
    first_name = (from_user.get('first_name') or '').strip()

    if not chat_id:
        if callback_id:
            answer_callback_query(callback_query_id=callback_id)
        return

    try:
        if data == HELP_CALLBACK_NO:
            try:
                answer_callback_query(callback_query_id=callback_id)
            except Exception:
                logger.warning('Stale help callback answer ignored for chat_id=%s', chat_id)
            send_bot_message(chat_id=chat_id, text=help_back_text())
            return

        if data != HELP_CALLBACK_YES:
            try:
                answer_callback_query(callback_query_id=callback_id)
            except Exception:
                logger.warning('Stale help callback answer ignored for chat_id=%s', chat_id)
            return

        _upsert_contact(chat_id, username, first_name)
        try:
            answer_callback_query(callback_query_id=callback_id, text='Оператор вызван')
        except Exception:
            pass
        send_bot_message(chat_id=chat_id, text=help_operator_called_text())
        notify_admin_operator_called(chat_id=chat_id, username=username, first_name=first_name)
    except Exception:
        logger.exception('Help callback failed for chat_id=%s', chat_id)
        if callback_id:
            try:
                answer_callback_query(
                    callback_query_id=callback_id,
                    text='Не удалось вызвать оператора. Попробуйте позже.',
                    show_alert=True,
                )
            except Exception:
                logger.exception('Failed to answer help callback for chat_id=%s', chat_id)


def _process_trial_callback(callback_query):
    callback_id = callback_query.get('id')
    data = (callback_query.get('data') or '').strip()
    from_user = callback_query.get('from') or {}
    chat = (callback_query.get('message') or {}).get('chat') or {}
    chat_id = chat.get('id')
    username = (from_user.get('username') or '').strip()
    first_name = (from_user.get('first_name') or '').strip()

    if not chat_id:
        if callback_id:
            answer_callback_query(callback_query_id=callback_id)
        return

    try:
        if data == TRIAL_CALLBACK_NO:
            try:
                answer_callback_query(callback_query_id=callback_id)
            except Exception:
                logger.warning('Stale callback answer ignored for chat_id=%s', chat_id)
            send_bot_message(chat_id=chat_id, text=trial_cancelled_text())
            return

        if data != TRIAL_CALLBACK_YES:
            try:
                answer_callback_query(callback_query_id=callback_id)
            except Exception:
                logger.warning('Stale callback answer ignored for chat_id=%s', chat_id)
            return

        _upsert_contact(chat_id, username, first_name)
        try:
            token = issue_trial_token(telegram_user_id=chat_id, username=username)
        except TrialCooldownError as exc:
            try:
                answer_callback_query(callback_query_id=callback_id)
            except Exception:
                pass
            send_bot_message(chat_id=chat_id, text=trial_cooldown_text(exc.available_at))
            return

        try:
            answer_callback_query(callback_query_id=callback_id, text='Токен отправлен')
        except Exception:
            pass
        notify_trial_token(chat_id=chat_id, token=token)
    except Exception:
        logger.exception('Trial callback failed for chat_id=%s', chat_id)
        if callback_id:
            try:
                answer_callback_query(
                    callback_query_id=callback_id,
                    text='Не удалось выдать токен. Попробуйте позже.',
                    show_alert=True,
                )
            except Exception:
                logger.exception('Failed to answer trial callback for chat_id=%s', chat_id)


def _process_callback(callback_query):
    data = (callback_query.get('data') or '').strip()
    if data in (HELP_CALLBACK_YES, HELP_CALLBACK_NO):
        _process_help_callback(callback_query)
        return
    if data in (TRIAL_CALLBACK_YES, TRIAL_CALLBACK_NO):
        _process_trial_callback(callback_query)
        return

    callback_id = callback_query.get('id')
    if callback_id:
        try:
            answer_callback_query(callback_query_id=callback_id)
        except Exception:
            logger.warning('Unknown callback ignored: %s', data)


def process_telegram_update(payload):
    try:
        log_incoming_update(payload)
    except Exception:
        logger.exception('Failed to log incoming Telegram update')

    callback_query = payload.get('callback_query')
    if callback_query:
        _process_callback(callback_query)
        return

    message = payload.get('message') or payload.get('edited_message') or {}
    if not message:
        return

    chat = message.get('chat') or {}
    chat_id = chat.get('id')
    if not chat_id:
        return

    from_user = message.get('from') or {}
    username = (chat.get('username') or from_user.get('username') or '').strip()
    first_name = (from_user.get('first_name') or '').strip()
    text = (message.get('text') or '').strip()
    command = _bot_command(text)

    if command == '/start':
        try:
            _handle_start(chat_id=chat_id, username=username, first_name=first_name, text=text)
        except Exception:
            logger.exception('Failed to reply to /start for chat_id=%s', chat_id)
    elif command == '/trial':
        try:
            _handle_trial_request(chat_id=chat_id, username=username, first_name=first_name)
        except Exception:
            logger.exception('Failed to reply to /trial for chat_id=%s', chat_id)
    elif command == '/referral':
        try:
            _handle_referral_request(chat_id=chat_id, username=username, first_name=first_name)
        except Exception:
            logger.exception('Failed to reply to /referral for chat_id=%s', chat_id)
    elif command == '/invited':
        try:
            _handle_invited_request(
                chat_id=chat_id,
                username=username,
                first_name=first_name,
                args=_command_args(text),
            )
        except Exception:
            logger.exception('Failed to reply to /invited for chat_id=%s', chat_id)
    elif command == '/tokens':
        try:
            _handle_tokens_request(chat_id=chat_id, username=username, first_name=first_name)
        except Exception:
            logger.exception('Failed to reply to /tokens for chat_id=%s', chat_id)
    elif command == '/help':
        try:
            _handle_help_request(chat_id=chat_id, username=username, first_name=first_name)
        except Exception:
            logger.exception('Failed to reply to /help for chat_id=%s', chat_id)
    elif message.get('document') and str(chat_id) == str(
        getattr(settings, 'GHOST_NOTE_ADMIN_CHAT_ID', '') or getattr(settings, 'OWNER_CHAT_ID', '')
    ):
        doc = message['document']
        logger.info(
            'Admin sent installer update: %s file_id=%s',
            doc.get('file_name'),
            doc.get('file_id'),
        )
