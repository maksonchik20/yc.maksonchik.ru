import html

from django.db import transaction
from django.utils import timezone

from .models import (
    GhostAccessToken,
    GhostTelegramContact,
    TEST_TOKEN_COOLDOWN,
    TEST_TOKEN_DURATION,
)

from .purchase_tips import (
    SUPPORT_TELEGRAM,
    SUPPORT_TELEGRAM_URL,
    download_telegram_html,
    usage_instructions_telegram_html,
)

from .referral_bot import get_or_create_referrer_user

TRIAL_CALLBACK_YES = 'trial_yes'
TRIAL_CALLBACK_NO = 'trial_no'


class TrialCooldownError(Exception):
    def __init__(self, available_at):
        self.available_at = available_at


def trial_available_at(contact):
    if not contact.last_trial_at:
        return None
    eligible_at = contact.last_trial_at + TEST_TOKEN_COOLDOWN
    if timezone.now() >= eligible_at:
        return None
    return eligible_at


def format_msk_datetime(dt):
    return timezone.localtime(dt).strftime('%d.%m.%Y %H:%M') + ' МСК'


def trial_confirm_text():
    minutes = int(TEST_TOKEN_DURATION.total_seconds() // 60)
    return (
        '🧪 <b>Пробный доступ Ghost Note</b>\n\n'
        f'{download_telegram_html()}\n\n'
        f'{usage_instructions_telegram_html()}\n\n'
        '<b>Важно:</b> сначала скачайте и запустите программу, '
        'убедитесь, что она работает — и только после этого получайте токен.\n\n'
        f'Токен будет активен <b>{minutes} минут</b> с момента выдачи.\n\n'
        'Выдать пробный токен?'
    )


def trial_confirm_keyboard():
    return {
        'inline_keyboard': [[
            {'text': 'Да', 'callback_data': TRIAL_CALLBACK_YES},
            {'text': 'Вернуться обратно', 'callback_data': TRIAL_CALLBACK_NO},
        ]],
    }


def trial_cooldown_text(available_at):
    return (
        '⏳ Пробный доступ уже использован.\n\n'
        f'Следующий запрос будет доступен после '
        f'<b>{format_msk_datetime(available_at)}</b>.\n\n'
        f'Если нужен ещё тестовый доступ раньше — напишите '
        f'<a href="{SUPPORT_TELEGRAM_URL}">{SUPPORT_TELEGRAM}</a>.\n\n'
        f'Полный доступ: <a href="https://yc.maksonchik.ru/buy/">купить на сайте</a>'
    )


def trial_cancelled_text():
    return (
        'Хорошо. Когда будете готовы — снова отправьте /trial.\n\n'
        'Полный доступ: https://yc.maksonchik.ru/buy/'
    )


def trial_issued_text(token):
    expires = format_msk_datetime(token.expires_at)
    minutes = int(TEST_TOKEN_DURATION.total_seconds() // 60)
    return (
        '✅ <b>Пробный доступ выдан</b>\n\n'
        f'Токен: <code>{html.escape(token.token)}</code>\n'
        f'Действует до: <b>{expires}</b>\n'
        f'Длительность: {minutes} мин\n\n'
        'Введите этот токен в программу Ghost Note.\n'
        'Сохраните токен — он нужен для входа.\n'
        'Повторный пробный доступ — не чаще одного раза в неделю.\n\n'
        f'Если нужен ещё тестовый доступ — напишите '
        f'<a href="{SUPPORT_TELEGRAM_URL}">{SUPPORT_TELEGRAM}</a>.'
    )


@transaction.atomic
def issue_trial_token(*, telegram_user_id, username=''):
    contact = GhostTelegramContact.objects.select_for_update().get(
        telegram_user_id=telegram_user_id,
    )
    available_at = trial_available_at(contact)
    if available_at:
        raise TrialCooldownError(available_at)

    now = timezone.now()
    user = get_or_create_referrer_user(chat_id=telegram_user_id, username=username)
    token = GhostAccessToken.objects.create(
        user=user,
        token_type=GhostAccessToken.TokenType.TEST,
        starts_at=now,
        expires_at=now + TEST_TOKEN_DURATION,
        allow_local=True,
        allow_remote=True,
        is_active=True,
    )
    contact.last_trial_at = now
    contact.save(update_fields=['last_trial_at'])
    return token
