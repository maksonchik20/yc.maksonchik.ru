import json
import logging

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from .forms import PurchaseForm
from .models import GhostPurchaseOrder
from .purchase import create_order_from_form, fulfill_purchase_order
from .telegram_notify import (
    notify_admin_form_attempt,
    notify_admin_purchase_attempt,
    notify_admin_purchase_failed,
)
from .yookassa_client import YooKassaError, create_payment, is_yookassa_webhook_ip

logger = logging.getLogger(__name__)


def _site_base_url(request):
    return request.build_absolute_uri('/').rstrip('/')


def _buy_context(form):
    bot_username = getattr(settings, 'GHOST_NOTE_BOT_USERNAME', '')
    bot_url = f'https://t.me/{bot_username.lstrip("@")}' if bot_username else ''
    return {
        'form': form,
        'ghost_bot_username': bot_username,
        'ghost_bot_url': bot_url,
    }


@require_http_methods(['GET', 'POST'])
def buy(request):
    if request.method == 'GET':
        initial = {}
        referral_key = (request.GET.get('ref') or '').strip().upper()
        if referral_key:
            initial['referral_key'] = referral_key
        form = PurchaseForm(initial=initial)
        return render(request, 'ghost_note/buy.html', _buy_context(form))

    form = PurchaseForm(request.POST)
    if not form.is_valid():
        notify_admin_form_attempt(
            customer_name=form.data.get('customer_name', ''),
            customer_telegram=form.data.get('customer_telegram', ''),
            customer_email=form.data.get('customer_email', ''),
            errors=[str(error) for errors in form.errors.values() for error in errors],
        )
        return render(request, 'ghost_note/buy.html', _buy_context(form), status=400)

    try:
        order = create_order_from_form(
            customer_name=form.cleaned_data['customer_name'],
            customer_telegram=form.cleaned_data['customer_telegram'],
            customer_email=form.cleaned_data['customer_email'],
            referral_key=form.cleaned_data['referral_key'],
            access_type=form.cleaned_data['access_type'],
            duration_minutes=form.cleaned_data['duration_minutes'],
            starts_at=form.cleaned_data['starts_at'],
        )
    except ValueError as exc:
        form.add_error('referral_key', str(exc))
        notify_admin_form_attempt(
            customer_name=form.cleaned_data.get('customer_name', ''),
            customer_telegram=form.cleaned_data.get('customer_telegram', ''),
            customer_email=form.cleaned_data.get('customer_email', ''),
            errors=[str(exc)],
        )
        return render(request, 'ghost_note/buy.html', _buy_context(form), status=400)

    notify_admin_purchase_attempt(order, note='Создан заказ, формируем ссылку на оплату…')

    return_url = request.build_absolute_uri(
        reverse('ghost_buy_success', kwargs={'public_id': order.public_id})
    )
    description = f'Ghost Note — {order.get_access_type_display()}'

    try:
        payment = create_payment(
            amount=order.amount,
            description=description,
            return_url=return_url,
            metadata={'order_id': str(order.public_id)},
        )
    except YooKassaError as exc:
        logger.exception('YooKassa create payment failed')
        order.status = GhostPurchaseOrder.Status.FAILED
        order.save(update_fields=['status'])
        notify_admin_purchase_failed(order, reason=str(exc))
        form.add_error(None, f'Не удалось создать платёж: {exc}')
        return render(request, 'ghost_note/buy.html', _buy_context(form), status=502)

    order.yookassa_payment_id = payment['id']
    order.save(update_fields=['yookassa_payment_id'])

    confirmation_url = payment.get('confirmation_url')
    if not confirmation_url:
        order.status = GhostPurchaseOrder.Status.FAILED
        order.save(update_fields=['status'])
        notify_admin_purchase_failed(order, reason='ЮKassa не вернула ссылку на оплату')
        form.add_error(None, 'ЮKassa не вернула ссылку на оплату.')
        return render(request, 'ghost_note/buy.html', _buy_context(form), status=502)

    notify_admin_purchase_attempt(order, note='Покупатель перенаправлен на оплату')

    return redirect(confirmation_url)


@require_GET
def buy_success(request, public_id):
    order = get_object_or_404(GhostPurchaseOrder, public_id=public_id)
    token_value = ''
    if order.token_id:
        token_value = order.token.token
    elif order.yookassa_payment_id:
        try:
            fulfill_purchase_order(
                order,
                payment_id=order.yookassa_payment_id,
                verify_remote=True,
            )
            order.refresh_from_db()
            if order.token_id:
                token_value = order.token.token
        except YooKassaError:
            logger.exception('Payment verify on success page failed')

    return render(request, 'ghost_note/buy_success.html', {
        'order': order,
        'token': token_value,
        'status_url': reverse('ghost_buy_status', kwargs={'public_id': order.public_id}),
    })


@require_GET
def buy_status(request, public_id):
    order = get_object_or_404(GhostPurchaseOrder, public_id=public_id)
    token_value = ''
    if order.status != GhostPurchaseOrder.Status.PAID and order.yookassa_payment_id:
        try:
            fulfill_purchase_order(
                order,
                payment_id=order.yookassa_payment_id,
                verify_remote=True,
            )
            order.refresh_from_db()
        except YooKassaError:
            logger.exception('Payment verify on status poll failed')

    if order.token_id:
        token_value = order.token.token

    return JsonResponse({
        'status': order.status,
        'paid': order.status == GhostPurchaseOrder.Status.PAID,
        'token': token_value,
        'access_type': order.access_type,
        'starts_at': order.starts_at.isoformat() if order.starts_at else '',
        'expires_at': order.token.expires_at.isoformat() if order.token_id else '',
        'telegram_sent': bool(order.telegram_notified_at),
        'email_sent': bool(order.email_notified_at),
    })


@csrf_exempt
@require_http_methods(['POST'])
def yookassa_webhook(request):
    client_ip = request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
    if not client_ip:
        client_ip = request.META.get('REMOTE_ADDR', '')
    if not is_yookassa_webhook_ip(client_ip):
        logger.warning('YooKassa webhook from unknown IP: %s', client_ip)

    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return HttpResponse(status=400)

    event = payload.get('event', '')
    payment_obj = payload.get('object') or {}
    payment_id = payment_obj.get('id', '')
    metadata = payment_obj.get('metadata') or {}
    order_public_id = metadata.get('order_id')

    if event == 'payment.succeeded' and order_public_id:
        try:
            order = GhostPurchaseOrder.objects.get(public_id=order_public_id)
            fulfill_purchase_order(order, payment_id=payment_id, verify_remote=True)
        except GhostPurchaseOrder.DoesNotExist:
            logger.error('Order not found for webhook: %s', order_public_id)
        except YooKassaError:
            logger.exception('Webhook payment verify failed for %s', payment_id)
    elif event == 'payment.canceled' and order_public_id:
        updated = GhostPurchaseOrder.objects.filter(
            public_id=order_public_id,
            status=GhostPurchaseOrder.Status.PENDING,
        ).update(status=GhostPurchaseOrder.Status.CANCELED)
        if updated:
            try:
                order = GhostPurchaseOrder.objects.get(public_id=order_public_id)
                notify_admin_purchase_failed(order, reason='Оплата отменена')
            except GhostPurchaseOrder.DoesNotExist:
                pass

    return HttpResponse(status=200)
