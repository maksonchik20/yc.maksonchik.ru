import json
import uuid
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from .models import GhostAccessToken, GhostSecuritySession, GhostSession, GhostTextMessage


@override_settings(GHOST_NOTE_BOT_TOKEN='', GHOST_NOTE_ADMIN_CHAT_ID='')
class ReliableTextDeliveryTests(TestCase):
    def setUp(self):
        self.token = GhostAccessToken.objects.create(
            token='TEXT01', token_type='real', payment_amount=1,
            starts_at=timezone.now() - timedelta(minutes=5),
            expires_at=timezone.now() + timedelta(hours=1))
        self.session = GhostSession.objects.create(access_token=self.token)
        self.security = GhostSecuritySession.objects.create(
            session=self.session, access_token=self.token, device_key_hash='a' * 64,
            producer_secret='b' * 64, viewer_secret='c' * 64,
            lease_expires_at=timezone.now() + timedelta(minutes=10))
        self.legacy_session = GhostSession.objects.create(access_token=self.token)

    def post_text(self, text='same [text] 😀', key=None, legacy=False, **fieldToOverride):
        session = self.legacy_session if legacy else self.session
        fieldToValue = {'session_id': str(session.session_id), 'text': text}
        if key is not None:
            fieldToValue['idempotency_key'] = key
        fieldToValue.update(fieldToOverride)
        return self.client.post('/ghost/text/' if legacy else '/ghost/v2/text/',
            data=json.dumps(fieldToValue, ensure_ascii=False), content_type='application/json',
            HTTP_X_GHOST_VIEWER=self.security.viewer_secret)

    def inbox(self, **headerToOverride):
        headerToValue = {'HTTP_X_GHOST_LEASE': self.security.producer_secret}
        headerToValue.update(headerToOverride)
        return self.client.get('/ghost/v2/text/inbox/',
            {'session_id': self.session.session_id}, **headerToValue)

    def ack(self, ids, **headerToOverride):
        headerToValue = {'HTTP_X_GHOST_LEASE': self.security.producer_secret}
        headerToValue.update(headerToOverride)
        return self.client.post('/ghost/v2/text/ack/', content_type='application/json',
            data=json.dumps({'session_id': str(self.session.session_id), 'message_ids': ids}),
            **headerToValue)

    def test_lost_inbox_response_does_not_consume_and_retry_returns_same_ids(self):
        self.assertEqual(self.post_text().json(), {'ok': True})
        first = self.inbox()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()['protocol'], 'ack-v1')
        self.assertEqual(first['Cache-Control'], 'no-store')
        self.assertEqual(first.json(), self.inbox().json())
        message = GhostTextMessage.objects.get()
        self.assertFalse(message.delivered)
        self.assertEqual(first.json()['messages'], [{'id': str(message.pk), 'text': message.text}])

    def test_lost_ack_response_can_be_retried_and_duplicate_ids_are_harmless(self):
        self.post_text()
        message_id = self.inbox().json()['messages'][0]['id']
        self.assertEqual(self.ack([message_id]).json(), {'ok': True})
        self.assertEqual(self.ack([message_id, message_id]).json(), {'ok': True})
        self.assertEqual(self.inbox().json(), {'protocol': 'ack-v1', 'messages': []})
        self.assertTrue(GhostTextMessage.objects.get().delivered)

    def test_partial_ack_preserves_remaining_messages_in_order(self):
        for text in ['first', 'second', 'third']:
            self.post_text(text)
        offered = self.inbox().json()['messages']
        self.assertEqual(self.ack([offered[0]['id']]).status_code, 200)
        self.assertEqual(self.inbox().json()['messages'], offered[1:])

    def test_identical_text_with_different_intents_has_distinct_ids(self):
        for _index in range(2):
            self.post_text(key=str(uuid.uuid4()))
        messages = self.inbox().json()['messages']
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]['text'], messages[1]['text'])
        self.assertNotEqual(messages[0]['id'], messages[1]['id'])

    def test_legacy_empty_and_control_character_text_remains_deliverable(self):
        texts = ['', 'left\x00right\b\f\t\r\n', 'next message']
        for text in texts:
            self.assertEqual(self.post_text(text).json(), {'ok': True})
        messages = self.inbox().json()['messages']
        self.assertEqual([message['text'] for message in messages], texts)
        self.assertEqual(self.ack([message['id'] for message in messages]).status_code, 200)
        self.assertEqual(self.inbox().json()['messages'], [])

    def test_post_retry_after_response_loss_deduplicates_for_both_viewers(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                key = str(uuid.uuid4())
                before = GhostTextMessage.objects.count()
                self.assertEqual(self.post_text(key=key, legacy=legacy).json(), {'ok': True})
                self.assertEqual(self.post_text(key=key, legacy=legacy).json(), {'ok': True})
                self.assertEqual(GhostTextMessage.objects.count(), before + 1)
                # Retrying a sent intent must not put it back in the queue.
                GhostTextMessage.objects.filter(event_id=key).update(delivered=True)
                self.assertEqual(self.post_text(key=key, legacy=legacy).status_code, 200)
                self.assertTrue(GhostTextMessage.objects.get(event_id=key).delivered)

    def test_changed_intent_same_key_is_conflict_not_silent_data_loss(self):
        for legacy in (False, True):
            key = str(uuid.uuid4())
            self.post_text('original', key=key, legacy=legacy)
            response = self.post_text('changed', key=key, legacy=legacy)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()['error'], 'idempotency_conflict')
            self.assertEqual(GhostTextMessage.objects.get(event_id=key).text, 'original')

    def test_key_is_scoped_to_session_and_does_not_reuse_activity_events(self):
        key = str(uuid.uuid4())
        self.post_text('v2', key=key)
        self.post_text('v1', key=key, legacy=True)
        self.assertEqual(GhostTextMessage.objects.filter(event_id=key).count(), 2)
        event_id = uuid.uuid4()
        GhostTextMessage.objects.create(session=self.session, event_id=event_id, source='local', text='local')
        self.assertEqual(self.post_text('local', key=str(event_id)).status_code, 409)

    def test_malformed_ids_never_consume_messages(self):
        self.post_text()
        good_id = self.inbox().json()['messages'][0]['id']
        for ids in (None, [], good_id, [1], [True], ['0'], ['01'], ['-1'], ['1.0'],
                    ['9223372036854775808'], [good_id] * 51, [good_id, 'invalid']):
            with self.subTest(ids=ids):
                self.assertEqual(self.ack(ids).status_code, 400)
                self.assertFalse(GhostTextMessage.objects.get().delivered)

    def test_unknown_or_cross_session_ack_does_not_partially_consume(self):
        self.post_text()
        good_id = str(GhostTextMessage.objects.get().pk)
        other = GhostTextMessage.objects.create(session=self.legacy_session, text='other session')
        for unknown_id in (str(other.pk), '9223372036854775807'):
            self.assertEqual(self.ack([good_id, unknown_id]).status_code, 400)
        self.assertFalse(GhostTextMessage.objects.filter(delivered=True).exists())

    def test_viewer_cannot_fetch_or_ack_and_producer_cannot_send(self):
        self.post_text()
        message_id = str(GhostTextMessage.objects.get().pk)
        self.assertEqual(self.inbox(HTTP_X_GHOST_LEASE=self.security.viewer_secret).status_code, 401)
        self.assertEqual(self.ack([message_id], HTTP_X_GHOST_LEASE=self.security.viewer_secret).status_code, 401)
        self.assertEqual(self.client.post('/ghost/v2/text/', content_type='application/json',
            data=json.dumps({'session_id': str(self.session.session_id), 'text': 'forbidden'}),
            HTTP_X_GHOST_VIEWER=self.security.producer_secret).status_code, 401)
        self.assertFalse(GhostTextMessage.objects.get().delivered)

    def test_invalid_expired_revoked_and_changed_permissions_keep_messages_pending(self):
        self.post_text()
        message_id = str(GhostTextMessage.objects.get().pk)
        self.assertEqual(self.inbox(HTTP_X_GHOST_LEASE='invalid').status_code, 401)
        for fieldToValue in ({'revoked_at': timezone.now()},
                             {'lease_expires_at': timezone.now() - timedelta(seconds=1)}):
            GhostSecuritySession.objects.filter(pk=self.security.pk).update(**fieldToValue)
            self.assertEqual(self.inbox().status_code, 401)
            self.assertEqual(self.ack([message_id]).status_code, 401)
            GhostSecuritySession.objects.filter(pk=self.security.pk).update(
                revoked_at=None, lease_expires_at=timezone.now() + timedelta(minutes=10))
        for fieldToValue, status in (({'is_active': False}, 401),
                                    ({'allow_remote': False, 'allow_ai': False}, 403)):
            GhostAccessToken.objects.filter(pk=self.token.pk).update(**fieldToValue)
            self.assertEqual(self.inbox().status_code, status)
            self.assertEqual(self.ack([message_id]).status_code, status)
            GhostAccessToken.objects.filter(pk=self.token.pk).update(is_active=True, allow_remote=True, allow_ai=True)
        self.assertFalse(GhostTextMessage.objects.get().delivered)

    def test_ai_only_client_can_read_text_without_remote_audio_permission(self):
        GhostAccessToken.objects.filter(pk=self.token.pk).update(allow_remote=False, allow_ai=True)
        self.assertEqual(self.post_text().status_code, 200)
        messages = self.inbox().json()['messages']
        self.assertEqual(self.ack([messages[0]['id']]).status_code, 200)

    def test_inbox_batch_limit_byte_budget_and_identical_retries(self):
        for index in range(51):
            GhostTextMessage.objects.create(session=self.session, text=str(index))
        first = self.inbox().json()['messages']
        self.assertEqual(len(first), 50)
        self.assertEqual(self.inbox().json()['messages'], first)
        self.ack([item['id'] for item in first])
        self.assertEqual(self.inbox().json()['messages'][0]['text'], '50')
        GhostTextMessage.objects.filter(session=self.session).update(delivered=True)
        for _index in range(10):
            self.post_text('я' * 30000, key=str(uuid.uuid4()))
        response = self.inbox()
        self.assertLess(len(response.content), 256 * 1024)
        self.assertGreater(len(response.json()['messages']), 0)
        self.assertLess(len(response.json()['messages']), 10)

    def test_invalid_idempotency_keys_and_oversized_new_requests_rejected(self):
        for legacy in (False, True):
            for key in ('', 'not-a-uuid', '1' * 32, 123, {'key': 'bad'}):
                with self.subTest(legacy=legacy, key=key):
                    self.assertEqual(self.post_text(key=key, legacy=legacy).status_code, 400)
            response = self.post_text('x' * 65537, key=str(uuid.uuid4()), legacy=legacy)
            self.assertIn(response.status_code, (400, 413))
        self.assertEqual(GhostTextMessage.objects.count(), 0)

    def test_old_windows_and_macos_v2_contract_stays_string_array_and_consume_once(self):
        texts = ['[a] 😀', 'identical', 'identical', 'line\nquote"\\ tail']
        for text in texts:
            self.assertEqual(self.post_text(text).json(), {'ok': True})
        response = self.client.get('/ghost/v2/text/poll/', {'session_id': self.session.session_id},
            HTTP_X_GHOST_LEASE=self.security.producer_secret)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'messages': texts})
        self.assertTrue(all(isinstance(item, str) for item in response.json()['messages']))
        self.assertFalse(GhostTextMessage.objects.filter(delivered=False).exists())
        self.assertEqual(self.client.get('/ghost/v2/text/poll/', {'session_id': self.session.session_id},
            HTTP_X_GHOST_LEASE=self.security.producer_secret).json(), {'messages': []})

    def test_old_windows_v1_contract_retains_bracket_workaround_without_ack(self):
        texts = ['[a] tail', '[]', 'same', 'same']
        for text in texts:
            self.assertEqual(self.post_text(text, legacy=True).json(), {'ok': True})
        response = self.client.get('/ghost/text/poll/', {'session_id': self.legacy_session.session_id},
            HTTP_X_ACCESS_TOKEN=self.token.token)
        self.assertEqual(response.json(), {'messages': texts})
        self.assertEqual(response.content.count(b'['), 1)
        self.assertEqual(response.content.count(b']'), 1)
        self.assertFalse(GhostTextMessage.objects.filter(delivered=False).exists())

    def test_client_downgrade_can_consume_unacknowledged_messages_via_legacy_v2(self):
        self.post_text('pending across downgrade')
        self.inbox()  # A newer client stopped before ACK; an older one reconnects.
        response = self.client.get('/ghost/v2/text/poll/', {'session_id': self.session.session_id},
            HTTP_X_GHOST_LEASE=self.security.producer_secret)
        self.assertEqual(response.json(), {'messages': ['pending across downgrade']})
        self.assertEqual(self.inbox().json()['messages'], [])
