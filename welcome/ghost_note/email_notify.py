import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone

from .models import GhostPurchaseOrder
from .purchase_tips import post_purchase_email_plain, post_purchase_email_subject

logger = logging.getLogger(__name__)


def is_email_configured():
    return bool(
        getattr(settings, 'EMAIL_HOST_USER', '')
        and getattr(settings, 'EMAIL_HOST_PASSWORD', '')
    )


def format_purchase_email(order, token_value):
    starts_at = timezone.localtime(order.starts_at)
    return post_purchase_email_plain(
        customer_name=order.customer_name,
        token_value=token_value,
        access_type=order.get_access_type_display(),
        starts_at=starts_at.strftime('%d.%m.%Y %H:%M'),
        duration_minutes=order.duration_minutes,
    )


def notify_purchase_email(order, *, token_value=None):
    """
    Отправляет токен покупателю на e-mail. Повторно не шлёт, если уже отправлено.
    """
    order = GhostPurchaseOrder.objects.select_related('token').get(pk=order.pk)

    if order.email_notified_at:
        return True

    if not order.customer_email:
        return False

    if not is_email_configured():
        order.email_notify_error = 'Почта не настроена (EMAIL_HOST_PASSWORD)'
        order.save(update_fields=['email_notify_error'])
        return False

    token_value = token_value or (order.token.token if order.token_id else '')
    if not token_value:
        return False

    subject = post_purchase_email_subject()
    body = format_purchase_email(order, token_value)
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', settings.EMAIL_HOST_USER)

    try:
        message = EmailMultiAlternatives(
            subject=subject,
            body=body,
            from_email=from_email,
            to=[order.customer_email],
        )
        message.send(fail_silently=False)
    except Exception as exc:
        logger.exception('Email notify failed for order %s', order.public_id)
        order.email_notify_error = str(exc)[:500]
        order.save(update_fields=['email_notify_error'])
        return False

    order.email_notified_at = timezone.now()
    order.email_notify_error = ''
    order.save(update_fields=['email_notified_at', 'email_notify_error'])
    return True
