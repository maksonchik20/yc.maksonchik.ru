import json
from decimal import Decimal
from unittest.mock import Mock, patch

import requests
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.utils import timezone

from .models import GhostAccessToken, GhostPurchaseOrder
from .payment_webhook_router import with_payment_webhook
from .purchase_views import yookassa_webhook
from .yookassa_client import YooKassaError


class RouterTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.telegram = Mock(return_value=HttpResponse('telegram'))
        self.route = with_payment_webhook(self.telegram)

    def test_payment_events_route_to_payment_handler_with_original_body(self):
        for event in ('payment.succeeded', 'payment.canceled'):
            payload = {'type': 'notification', 'event': event, 'object': {'id': 'fixture'}}
            request = self.factory.post('/webhook_tg/', data=json.dumps(payload), content_type='application/json')
            with patch('ghost_note.payment_webhook_router.yookassa_webhook', return_value=HttpResponse(status=503)) as payment:
                self.assertEqual(self.route(request).status_code, 503)
                payment.assert_called_once_with(request)
                self.assertEqual(json.loads(request.body), payload)
        self.telegram.assert_not_called()
        self.assertTrue(self.route.csrf_exempt)

    def test_regular_telegram_update_keeps_its_handler_and_response(self):
        for payload in ({'update_id': 1, 'message': {'text': '/start'}},
                        {'business_message': {'text': 'fixture'}},
                        {'event': 'payment.succeeded'}):
            request = self.factory.post('/webhook_tg/', data=json.dumps(payload), content_type='application/json')
            with patch('ghost_note.payment_webhook_router.yookassa_webhook') as payment:
                self.assertEqual(self.route(request).content, b'telegram')
                payment.assert_not_called()

    def test_non_post_and_invalid_json_are_left_to_telegram_handler(self):
        for request in (self.factory.get('/webhook_tg/'),
                        self.factory.post('/webhook_tg/', data=b'{broken', content_type='application/json')):
            self.assertEqual(self.route(request).content, b'telegram')

    def test_malformed_payment_event_cannot_issue_access(self):
        for obj in ({}, {'metadata': {'order_id': 'fixture'}}, [], 'wrong'):
            request = self.factory.post('/webhook_tg/', data=json.dumps({
                'type': 'notification', 'event': 'payment.succeeded', 'object': obj,
            }), content_type='application/json', REMOTE_ADDR='77.75.153.78')
            with patch('ghost_note.purchase_views.fulfill_purchase_order') as fulfill:
                self.assertEqual(self.route(request).status_code, 400)
                fulfill.assert_not_called()
        self.telegram.assert_not_called()


class FulfillmentTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.telegram = Mock(return_value=HttpResponse('telegram'))
        self.route = with_payment_webhook(self.telegram)
        self.order = GhostPurchaseOrder.objects.create(
            customer_name='Fixture customer', customer_telegram='fixture_user',
            customer_email='fixture@example.invalid', access_type='local', duration_minutes=180,
            starts_at=timezone.now() + timezone.timedelta(hours=1), amount=Decimal('2500.00'),
            yookassa_payment_id='fixture-payment',
        )
        self.payload = {'type': 'notification', 'event': 'payment.succeeded', 'object': {
            'id': 'fixture-payment', 'metadata': {'order_id': str(self.order.public_id)},
        }}
        self.payment = {'id': 'fixture-payment', 'status': 'succeeded', 'paid': True,
            'amount': {'value': '2500.00', 'currency': 'RUB'},
            'metadata': {'order_id': str(self.order.public_id)}}

    def request(self):
        return self.factory.post('/webhook_tg/', data=json.dumps(self.payload),
                                 content_type='application/json', REMOTE_ADDR='77.75.153.78')

    def test_legacy_url_fulfills_paid_order_and_duplicate_does_not_create_another_token(self):
        def mark_telegram(order, **kwargs):
            GhostPurchaseOrder.objects.filter(pk=order.pk).update(telegram_notified_at=timezone.now())
        def mark_email(order, **kwargs):
            GhostPurchaseOrder.objects.filter(pk=order.pk).update(email_notified_at=timezone.now())
        with patch('ghost_note.purchase.get_payment', return_value=self.payment) as verify, \
             patch('ghost_note.purchase.notify_purchase_token', side_effect=mark_telegram) as telegram, \
             patch('ghost_note.purchase.notify_purchase_email', side_effect=mark_email) as email, \
             patch('ghost_note.purchase.notify_referrer_purchase'), \
             patch('ghost_note.purchase.notify_admin_purchase_paid') as admin:
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(self.route(self.request()).status_code, 200)
            self.order.refresh_from_db()
            token_id = self.order.token_id
            self.assertEqual(self.order.status, 'paid')
            self.assertIsNotNone(token_id)
            self.assertEqual(self.order.token.expires_at, self.order.starts_at + timezone.timedelta(minutes=180))
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(self.route(self.request()).status_code, 200)
            self.order.refresh_from_db()
            self.assertEqual(self.order.token_id, token_id)
            self.assertEqual(GhostAccessToken.objects.count(), 1)
            telegram.assert_called_once()
            email.assert_called_once()
            admin.assert_called_once()
            verify.assert_called_once_with('fixture-payment')
        self.telegram.assert_not_called()

    def test_gateway_and_network_errors_return_503_on_both_urls_for_retry(self):
        for handler in (self.route, yookassa_webhook):
            for error in (YooKassaError('temporary gateway failure'), requests.Timeout('temporary timeout')):
                with patch('ghost_note.purchase.get_payment', side_effect=error), \
                     patch('ghost_note.purchase_views.logger.exception'):
                    self.assertEqual(handler(self.request()).status_code, 503)
                self.order.refresh_from_db()
                self.assertEqual(self.order.status, 'pending')
                self.assertIsNone(self.order.token_id)

    def test_unpaid_remote_payment_does_not_fulfill(self):
        with patch('ghost_note.purchase.get_payment', return_value={**self.payment, 'status': 'pending', 'paid': False}):
            self.assertEqual(self.route(self.request()).status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'pending')
        self.assertIsNone(self.order.token_id)

    def test_another_orders_payment_does_not_fulfill(self):
        with patch('ghost_note.purchase.get_payment', return_value={**self.payment, 'metadata': {'order_id': 'another-order'}}):
            self.assertEqual(self.route(self.request()).status_code, 200)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.token_id)
