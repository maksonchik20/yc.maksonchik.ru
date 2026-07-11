import html

from django.db.models import F, Q
from django.utils import timezone

from .models import GhostAccessToken, GhostPurchaseOrder, GhostUser
from .referral_bot import get_or_create_referrer_user
from .telegram_notify import normalize_telegram_input, parse_telegram_recipient


def _format_dt(dt):
    return timezone.localtime(dt).strftime('%d.%m.%Y %H:%M') + ' МСК'


def _token_status(token, now):
    if now < token.starts_at:
        return 'ожидает начала'
    return 'активен'


def _token_kind(token):
    if token.token_type == GhostAccessToken.TokenType.TEST:
        return 'Тестовый'
    return 'Купленный'


def _access_modes(token):
    modes = []
    if token.allow_local:
        modes.append('локальный')
    if token.allow_remote:
        modes.append('удалённый')
    return ', '.join(modes) or '—'


def _user_ids_for_telegram(*, chat_id, username):
    user = get_or_create_referrer_user(chat_id=chat_id, username=username)
    user_ids = {user.pk}

    if username:
        user_ids.update(
            GhostUser.objects.filter(telegram_username__iexact=username).values_list('pk', flat=True)
        )

    telegram_values = {str(chat_id)}
    if username:
        telegram_values.add(f'@{username}')
        telegram_values.add(username)

    for value in telegram_values:
        chat = parse_telegram_recipient(value)
        if chat:
            telegram_values.add(str(chat))

    order_filter = Q()
    for value in telegram_values:
        normalized = normalize_telegram_input(value)
        if not normalized:
            continue
        order_filter |= Q(customer_telegram__iexact=value)
        order_filter |= Q(customer_telegram__iexact=f'@{normalized}')
        order_filter |= Q(customer_telegram__iexact=normalized)
        if normalized.lstrip('-').isdigit():
            order_filter |= Q(customer_telegram__iexact=normalized.lstrip('-'))

    if order_filter:
        user_ids.update(
            GhostPurchaseOrder.objects.filter(
                order_filter,
                status=GhostPurchaseOrder.Status.PAID,
            ).exclude(user_id=None).values_list('user_id', flat=True)
        )

    return user_ids


def active_tokens_for_telegram(*, chat_id, username):
    now = timezone.now()
    user_ids = _user_ids_for_telegram(chat_id=chat_id, username=username)
    return (
        GhostAccessToken.objects.filter(
            user_id__in=user_ids,
            starts_at__lte=F('expires_at'),
            expires_at__gt=now,
            is_active=True,
        )
        .select_related('user')
        .order_by('-starts_at')
    )


def purchased_tokens_text(*, chat_id, username):
    now = timezone.now()
    tokens = list(active_tokens_for_telegram(chat_id=chat_id, username=username))
    if not tokens:
        return (
            '🎫 <b>Ваши токены</b>\n\n'
            'Активных токенов не найдено.\n\n'
            'Пробный доступ: /trial\n'
            'Купить доступ: https://yc.maksonchik.ru/buy/'
        )

    lines = ['🎫 <b>Ваши токены</b>', '']
    for index, token in enumerate(tokens, start=1):
        lines.extend([
            f'<b>{index}.</b> <code>{html.escape(token.token)}</code>',
            f'Тип: {html.escape(_token_kind(token))}',
            f'Статус: {html.escape(_token_status(token, now))}',
            f'Режим: {html.escape(_access_modes(token))}',
            f'С {_format_dt(token.starts_at)} до {_format_dt(token.expires_at)}',
        ])
        if token.token_type == GhostAccessToken.TokenType.REAL and token.payment_amount is not None:
            lines.append(f'Оплата: {token.payment_amount:.0f} ₽')
        lines.append('')
    return '\n'.join(lines).rstrip()
