from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import GhostAccessToken, GhostPurchaseOrder, GhostUser
from .referral_bot import resolve_referrer_from_telegram_profile
from .referrals import normalize_user_name
from .email_notify import notify_purchase_email
from .telegram_notify import notify_admin_purchase_paid, notify_purchase_token, notify_referrer_purchase
from .yookassa_client import get_payment, price_for_access


def resolve_referrer(referral_key):
    key = (referral_key or '').strip().upper()
    if not key:
        return None
    return GhostUser.objects.filter(referral_key__iexact=key).first()


def get_or_create_customer(name, referrer=None):
    normalized = normalize_user_name(name) or name.strip()
    user = GhostUser.objects.filter(name__iexact=normalized).first()
    if user:
        if referrer and not user.referred_by_id:
            user.referred_by = referrer
            user.save(update_fields=['referred_by'])
        return user
    return GhostUser.objects.create(name=normalized, referred_by=referrer)


@transaction.atomic
def fulfill_purchase_order(order, *, payment_id=None, verify_remote=False):
    order = GhostPurchaseOrder.objects.select_for_update().get(pk=order.pk)

    if order.status == GhostPurchaseOrder.Status.PAID and order.token_id:
        if order.customer_telegram and not order.telegram_notified_at:
            transaction.on_commit(lambda: notify_purchase_token(order))
        if order.customer_email and not order.email_notified_at:
            transaction.on_commit(lambda: notify_purchase_email(order))
        if order.referrer_id and not order.referrer_notified_at:
            transaction.on_commit(lambda: notify_referrer_purchase(order))
        return order.token

    if verify_remote and payment_id:
        payment = get_payment(payment_id)
        if payment.get('status') != 'succeeded' or not payment.get('paid'):
            return None
        metadata = payment.get('metadata') or {}
        if str(metadata.get('order_id', '')) != str(order.public_id):
            return None

    referrer = order.referrer
    if not referrer and order.referral_key_input:
        referrer = resolve_referrer(order.referral_key_input)
        if referrer:
            order.referrer = referrer
            order.save(update_fields=['referrer'])

    user = get_or_create_customer(order.customer_name, referrer=referrer)
    order.user = user

    allow_local = True
    allow_remote = True
    expires_at = order.starts_at + timezone.timedelta(minutes=order.duration_minutes)

    token = GhostAccessToken.objects.create(
        user=user,
        token_type=GhostAccessToken.TokenType.REAL,
        payment_amount=order.amount,
        label=user.name,
        starts_at=order.starts_at,
        expires_at=expires_at,
        allow_local=allow_local,
        allow_remote=allow_remote,
        is_active=True,
    )

    order.token = token
    order.status = GhostPurchaseOrder.Status.PAID
    order.paid_at = timezone.now()
    if payment_id:
        order.yookassa_payment_id = payment_id
    order.save(update_fields=[
        'user', 'token', 'status', 'paid_at', 'yookassa_payment_id', 'referrer',
    ])

    def _send_notifications():
        notify_purchase_token(order, token_value=token.token)
        notify_purchase_email(order, token_value=token.token)
        notify_referrer_purchase(order)
        notify_admin_purchase_paid(order, token_value=token.token)

    transaction.on_commit(_send_notifications)
    return token


def create_order_from_form(*, customer_name, customer_telegram, customer_email, referral_key, access_type, duration_minutes, starts_at):
    referrer = resolve_referrer(referral_key)
    if referral_key and not referrer:
        raise ValueError('Реферальный ключ не найден')
    if not referrer and customer_telegram:
        referrer = resolve_referrer_from_telegram_profile(customer_telegram)

    amount = price_for_access(access_type)
    return GhostPurchaseOrder.objects.create(
        customer_name=customer_name.strip(),
        customer_telegram=(customer_telegram or '').strip(),
        customer_email=(customer_email or '').strip(),
        referral_key_input=(referral_key or '').strip().upper(),
        referrer=referrer,
        access_type=access_type,
        duration_minutes=duration_minutes,
        starts_at=starts_at,
        amount=amount,
    )
