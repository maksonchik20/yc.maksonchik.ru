from datetime import timedelta
from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from . import telegram_notify as transport
from .models import GhostTelegramOutbox
from .outbox import TelegramAPIError, process_outbox


class TransportFixture:
    def setUp(self):
        super().setUp()
        for target, replacement in (
            ('_endpointBaseToRetryAt', {}),
            ('TELEGRAM_SESSION', Mock()),
        ):
            replacement_patch = patch.object(transport, target, replacement)
            replacement_patch.start()
            self.addCleanup(replacement_patch.stop)
        clock_patch = patch.object(transport.time, 'monotonic', return_value=100.0)
        self.clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.post = transport.TELEGRAM_SESSION.post
        self.post.return_value = self.response()

    def response(self, code=200, **responseKeyToValue):
        response = Mock(status_code=code)
        response.json.return_value = responseKeyToValue or {'ok': True, 'result': {'message_id': 10}}
        return response

    def urls(self):
        return [call.args[0].split('/bot')[0] for call in self.post.call_args_list]


@override_settings(GHOST_NOTE_BOT_TOKEN='test-secret')
class TelegramTransportTests(TransportFixture, SimpleTestCase):
    def test_primary_success_uses_verified_https_and_split_timeout(self):
        resultKeyToValue = transport._call_telegram_api('getMe', {})
        self.assertTrue(resultKeyToValue['ok'])
        self.assertEqual(self.urls(), ['https://api.telegram.org'])
        self.assertEqual(self.post.call_args.kwargs['timeout'], (2, 8))
        self.assertTrue(self.post.call_args.kwargs['verify'])
        self.assertIsNone(self.post.call_args.kwargs['headers'])

    def test_connect_and_read_timeout_try_both_fallbacks(self):
        self.post.side_effect = [requests.ConnectTimeout('offline'), requests.ReadTimeout('slow'), self.response()]
        transport._call_telegram_api('sendMessage', {'chat_id': 123, 'text': 'hello'}, timeout=5)
        self.assertEqual(self.urls(), ['https://api.telegram.org', 'https://149.154.167.220', 'https://149.154.167.99'])
        self.assertEqual([call.kwargs['timeout'] for call in self.post.call_args_list], [(2, 5), (.75, 5), (.75, 5)])
        self.assertEqual(self.post.call_args.kwargs['headers'], {'Host': 'api.telegram.org'})
        self.assertFalse(self.post.call_args.kwargs['verify'])
        self.assertEqual(transport._endpointBaseToRetryAt, {'https://api.telegram.org': 130, 'https://149.154.167.220': 130})

    def test_failed_endpoint_skipped_until_cooldown_then_reused(self):
        self.post.side_effect = [requests.ConnectionError('offline'), self.response()]
        transport._call_telegram_api('getMe', {})
        self.post.reset_mock(side_effect=True)
        self.clock.return_value = 129
        transport._call_telegram_api('getMe', {})
        self.assertEqual(self.urls(), ['https://149.154.167.220'])
        self.post.reset_mock()
        self.clock.return_value = 130
        transport._call_telegram_api('getMe', {})
        self.assertEqual(self.urls(), ['https://api.telegram.org'])
        self.assertEqual(transport._endpointBaseToRetryAt, {})

    def test_all_failed_endpoints_fail_fast_during_cooldown(self):
        self.post.side_effect = requests.ConnectTimeout('offline')
        with self.assertRaises(RuntimeError):
            transport._call_telegram_api('sendMessage', {})
        self.assertEqual(self.post.call_count, 3)
        self.post.reset_mock()
        with self.assertRaisesRegex(RuntimeError, 'circuit breaker'):
            transport._call_telegram_api('sendMessage', {})
        self.post.assert_not_called()

    def test_blocked_user_does_not_try_another_ip(self):
        self.post.return_value = self.response(403, ok=False, error_code=403, description='Forbidden: bot was blocked by the user')
        with self.assertRaises(TelegramAPIError) as caught:
            transport._call_telegram_api('sendMessage', {})
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(transport._endpointBaseToRetryAt, {})

    def test_rate_limit_preserves_retry_after_without_rotating_ips(self):
        self.post.return_value = self.response(429, ok=False, error_code=429, description='Too Many Requests', parameters={'retry_after': 120})
        with self.assertRaises(TelegramAPIError) as caught:
            transport._call_telegram_api('sendMessage', {})
        self.assertEqual(caught.exception.retry_after, 120)
        self.assertEqual(self.post.call_count, 1)

    def test_server_error_goes_back_to_outbox_not_another_ip(self):
        self.post.return_value = self.response(502, ok=False, description='Bad Gateway')
        with self.assertRaises(TelegramAPIError) as caught:
            transport._call_telegram_api('sendMessage', {})
        self.assertEqual(caught.exception.code, 502)
        self.assertEqual(self.post.call_count, 1)

    def test_invalid_json_not_immediately_resent(self):
        self.post.return_value.json.side_effect = ValueError('invalid')
        with self.assertRaisesRegex(RuntimeError, 'invalid JSON'):
            transport._call_telegram_api('sendMessage', {})
        self.assertEqual(self.post.call_count, 1)

    def test_polling_wait_does_not_become_connect_timeout(self):
        from .management.commands.ghost_telegram_poll import POLL_TIMEOUT
        self.assertEqual(POLL_TIMEOUT, 15)
        transport.get_telegram_updates(offset=42, poll_timeout=POLL_TIMEOUT)
        self.assertEqual(self.post.call_args.kwargs['timeout'], (2, 25))
        self.assertEqual(self.post.call_args.kwargs['json']['timeout'], 15)
        self.assertEqual(self.post.call_args.kwargs['json']['offset'], 42)

    def test_no_token_leaks_in_exception_or_logs(self):
        self.post.side_effect = requests.ConnectTimeout('https://api.telegram.org/bottest-secret/sendMessage')
        with self.assertLogs(transport.logger, level='WARNING') as logs:
            with self.assertRaises(RuntimeError) as caught:
                transport._call_telegram_api('sendMessage', {})
        self.assertNotIn('test-secret', str(caught.exception))
        self.assertNotIn('test-secret', '\n'.join(logs.output))

    @override_settings(TELEGRAM_API_ENDPOINTS=({'base': 'https://api.telegram.org'},), TELEGRAM_API_TIMEOUT=12)
    def test_existing_endpoint_override_keeps_default_connect_timeout(self):
        transport._call_telegram_api('getMe', {})
        self.assertEqual(self.post.call_args.kwargs['timeout'], (2, 12))

    @override_settings(GHOST_NOTE_BOT_TOKEN='')
    def test_missing_token_never_calls_network(self):
        with self.assertRaisesRegex(RuntimeError, 'GHOST_NOTE_BOT_TOKEN'):
            transport._call_telegram_api('sendMessage', {})
        self.post.assert_not_called()


@override_settings(GHOST_NOTE_BOT_TOKEN='test-secret', GHOST_NOTE_ADMIN_CHAT_ID='')
class OutboxTransportTests(TransportFixture, TransactionTestCase):
    def test_outage_retains_message_then_worker_delivers_after_recovery(self):
        self.post.side_effect = requests.ConnectTimeout('offline')
        resultKeyToValue = transport.send_bot_message(chat_id=123, text='hello', idempotency_key='outage-test')
        self.assertTrue(resultKeyToValue['queued'])
        self.assertEqual(self.post.call_count, 3)
        item = GhostTelegramOutbox.objects.get()
        GhostTelegramOutbox.objects.filter(pk=item.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        self.clock.return_value = 131
        self.post.reset_mock(side_effect=True)
        self.assertEqual(process_outbox()['sent'], 1)
        item.refresh_from_db()
        self.assertEqual(item.status, GhostTelegramOutbox.Status.SENT)
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(self.post.call_args.kwargs['timeout'], (2, 5))
        transport.send_bot_message(chat_id=123, text='hello', idempotency_key='outage-test')
        self.assertEqual(self.post.call_count, 1)

    def test_installer_uses_short_connect_and_send_timeout(self):
        self.post.side_effect = [requests.ConnectTimeout('offline'), self.response()]
        transport.send_bot_document(chat_id=123, file_id='existing-file-id')
        self.assertEqual(self.post.call_args.kwargs['timeout'], (.75, 5))
        self.assertEqual(self.post.call_args.kwargs['json']['document'], 'existing-file-id')
        self.assertEqual(GhostTelegramOutbox.objects.get().status, GhostTelegramOutbox.Status.SENT)

    def test_blocked_user_is_permanent_after_one_request(self):
        self.post.return_value = self.response(403, ok=False, error_code=403, description='Forbidden: bot was blocked by the user')
        transport.send_bot_message(chat_id=123, text='hello')
        self.assertEqual(GhostTelegramOutbox.objects.get().status, GhostTelegramOutbox.Status.FAILED)
        self.assertEqual(self.post.call_count, 1)

    def test_rate_limit_is_scheduled_once_by_outbox(self):
        self.post.return_value = self.response(429, ok=False, error_code=429, description='Too Many Requests', parameters={'retry_after': 120})
        transport.send_bot_message(chat_id=123, text='hello')
        item = GhostTelegramOutbox.objects.get()
        self.assertEqual(item.status, GhostTelegramOutbox.Status.PENDING)
        self.assertGreaterEqual((item.next_attempt_at - timezone.now()).total_seconds(), 119)
        self.assertEqual(self.post.call_count, 1)
