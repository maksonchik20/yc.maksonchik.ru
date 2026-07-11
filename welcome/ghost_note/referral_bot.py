from django.conf import settings

from django.db import transaction

from .models import GhostUser
from .purchase_tips import SUPPORT_TELEGRAM, SUPPORT_TELEGRAM_URL
from .referrals import REFERRAL_COMMISSION_RATE
from .telegram_notify import normalize_telegram_input

SITE_BUY_URL = 'https://yc.maksonchik.ru/buy/'
REF_START_PREFIX = 'ref_'


def _bot_username():
    return getattr(settings, 'GHOST_NOTE_BOT_USERNAME', '').lstrip('@').strip()


def referral_buy_url(referral_key):
    return f'{SITE_BUY_URL}?ref={referral_key}'


def referral_telegram_url(referral_key):
    username = _bot_username() or 'GhostNoteShopBot'
    return f'https://t.me/{username}?start={REF_START_PREFIX}{referral_key}'


def parse_referral_start_payload(text):
    parts = (text or '').split(maxsplit=1)
    if len(parts) < 2:
        return ''
    payload = parts[1].strip()
    if payload.lower().startswith(REF_START_PREFIX):
        return payload[len(REF_START_PREFIX):].strip().upper()
    if len(payload) == 8 and payload.isalnum():
        return payload.upper()
    return ''


@transaction.atomic
def get_or_create_referrer_user(*, chat_id, username=''):
    normalized_username = (username or '').lstrip('@').strip()

    if normalized_username:
        user = GhostUser.objects.select_for_update().filter(
            telegram_username__iexact=normalized_username,
        ).first()
        if user:
            return user

    display_name = f'@{normalized_username}' if normalized_username else f'TG {chat_id}'
    user = GhostUser.objects.select_for_update().filter(name__iexact=display_name).first()
    if user:
        if normalized_username and user.telegram_username.lower() != normalized_username.lower():
            user.telegram_username = normalized_username
            user.save(update_fields=['telegram_username'])
        return user

    return GhostUser.objects.create(
        name=display_name,
        telegram_username=normalized_username,
    )


def referral_info_text(user):
    commission_percent = int(REFERRAL_COMMISSION_RATE * 100)
    referral_key = user.referral_key
    buy_link = referral_buy_url(referral_key)
    tg_link = referral_telegram_url(referral_key)
    unpaid = user.unpaid_commission_total()
    total = user.total_commission()
    referrals_count = user.referrals.count()

    lines = [
        '🤝 <b>Реферальная программа Ghost Note</b>',
        '',
        f'Ваш реферальный ключ: <code>{referral_key}</code>',
        f'Ссылка в Telegram: <a href="{tg_link}">{tg_link}</a>',
        f'Ссылка на сайт: <a href="{buy_link}">{buy_link}</a>',
        '',
        '<b>Как это работает</b>',
        '1. Отправьте другу ссылку в Telegram или на сайт (или ключ).',
        '2. По TG-ссылке пригласивший сохранится автоматически; '
        'на сайте — ключ подставится в форму покупки.',
        f'3. После успешной оплаты вам начисляется <b>{commission_percent}%</b> от суммы покупки.',
        '',
        'Комиссия начисляется только за <b>оплаченные</b> доступы (не за пробный /trial).',
        'Выплаты — по договорённости, напишите '
        f'<a href="{SUPPORT_TELEGRAM_URL}">{SUPPORT_TELEGRAM}</a>.',
        '',
        f'Приглашено пользователей: <b>{referrals_count}</b>',
        f'Всего начислено: <b>{total:.2f} ₽</b>',
        f'К выплате: <b>{unpaid:.2f} ₽</b>',
    ]
    return '\n'.join(lines)


def invited_help_text():
    return (
        '👤 <b>Кто вас пригласил</b>\n\n'
        'Если вас пригласил друг, перейдите по его TG-ссылке или укажите ключ:\n\n'
        '<code>/invited КЛЮЧ</code>\n\n'
        'Пример: <code>/invited AB12CD34</code>\n\n'
        'Ключ можно также указать при покупке на сайте. '
        'Указать пригласившего можно <b>один раз</b>.'
    )


def _referrer_display(referrer):
    if referrer.telegram_username:
        return f'@{referrer.telegram_username}'
    return referrer.name


@transaction.atomic
def set_invited_by_key(*, chat_id, username, referral_key):
    user = get_or_create_referrer_user(chat_id=chat_id, username=username)
    key = (referral_key or '').strip().upper()
    if not key:
        return invited_help_text()

    if user.referred_by_id:
        return (
            'ℹ️ Пригласивший уже указан: '
            f'<b>{_referrer_display(user.referred_by)}</b> '
            f'(ключ <code>{user.referred_by.referral_key}</code>).\n\n'
            'Изменить нельзя. По вопросам — '
            f'<a href="{SUPPORT_TELEGRAM_URL}">{SUPPORT_TELEGRAM}</a>.'
        )

    referrer = GhostUser.objects.filter(referral_key__iexact=key).first()
    if not referrer:
        return (
            '❌ Реферальный ключ не найден.\n\n'
            'Проверьте ключ у друга (команда /referral) и отправьте снова:\n'
            f'<code>/invited {key}</code>'
        )

    if referrer.pk == user.pk:
        return '❌ Нельзя указать свой собственный ключ.'

    user.referred_by = referrer
    user.save(update_fields=['referred_by'])

    return (
        '✅ Пригласивший сохранён:\n'
        f'<b>{_referrer_display(referrer)}</b> '
        f'(ключ <code>{referrer.referral_key}</code>).\n\n'
        'При покупке на сайте ключ подставится автоматически, '
        'если укажете тот же Telegram.'
    )


def resolve_referrer_from_telegram_profile(customer_telegram):
    from .models import GhostTelegramContact

    normalized = normalize_telegram_input(customer_telegram)
    if not normalized:
        return None

    user = None
    if normalized.lstrip('-').isdigit():
        contact = GhostTelegramContact.objects.filter(
            telegram_user_id=int(normalized),
        ).first()
        if contact and contact.username:
            user = GhostUser.objects.filter(
                telegram_username__iexact=contact.username,
            ).first()
        if not user:
            user = GhostUser.objects.filter(name__iexact=f'TG {normalized}').first()
    else:
        user = GhostUser.objects.filter(telegram_username__iexact=normalized).first()

    if user and user.referred_by_id:
        return user.referred_by
    return None
