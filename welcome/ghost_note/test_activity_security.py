import base64
from datetime import timedelta
from io import BytesIO
import json
import uuid
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from .models import GhostAccessToken, GhostAIRequest, GhostSecuritySession, GhostTextMessage, GhostTokenDevice, GhostTokenScreenshot
from .test_security_v2 import SecurityFixture
from .test_ai import AIConversationTests


class DeviceLimitTests(SecurityFixture):
    def setUp(self):
        super().setUp()
        GhostAccessToken.objects.filter(pk=self.token.pk).update(max_devices=1, max_concurrent_sessions=1)

    def other_device(self):
        self.device = ec.generate_private_key(ec.SECP256R1())
        self.public_bytes = self.device.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

    def test_one_session_renewal_close_and_new_session(self):
        security, _ = self.authorize()
        self.authorize(security.session.session_id)
        self.authorize()
        security.refresh_from_db()
        self.assertIsNotNone(security.revoked_at)
        security = GhostSecuritySession.objects.filter(revoked_at__isnull=True).get()
        response = self.client.post('/ghost/v2/session/close/', data=b'', content_type='application/json',
            HTTP_X_SESSION_ID=security.session.session_id, HTTP_X_GHOST_LEASE=security.producer_secret)
        self.assertEqual(response.status_code, 200)
        self.authorize()

    def test_other_device_rejected_and_blocked_device_frees_slot(self):
        security, _ = self.authorize()
        original_private, original_public = self.device, self.public_bytes
        self.other_device()
        response = self.post('/ghost/v2/lease/', self.proof())
        self.assertEqual(response.json()['error'], 'device_limit')
        self.assertEqual(GhostTokenDevice.objects.count(), 1)
        GhostTokenDevice.objects.update(blocked=True)
        GhostSecuritySession.objects.update(revoked_at=timezone.now())
        self.authorize()
        self.device, self.public_bytes = original_private, original_public
        self.assertEqual(self.post('/ghost/v2/lease/', self.proof()).json()['error'], 'device_blocked')

    def test_concurrent_slot_limit_rejects_second_distinct_device(self):
        GhostAccessToken.objects.filter(pk=self.token.pk).update(max_devices=2)
        self.authorize()
        self.other_device()
        response = self.post('/ghost/v2/lease/', self.proof())
        self.assertEqual(response.json()['error'], 'session_limit')
        self.assertEqual(GhostTokenDevice.objects.count(), 1)

    def test_expired_session_no_longer_uses_slot(self):
        self.authorize()
        GhostSecuritySession.objects.update(lease_expires_at=timezone.now()-timedelta(seconds=1))
        self.authorize()

    def test_blocked_device_denied_in_all_protected_routes(self):
        security, _ = self.authorize()
        GhostTokenDevice.objects.update(blocked=True)
        response = self.client.get('/ghost/v2/activity/state/', HTTP_X_SESSION_ID=security.session.session_id,
                                   HTTP_X_GHOST_LEASE=security.producer_secret)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error'], 'device_blocked')

    def test_legacy_auth_and_viewer_still_work(self):
        response = self.client.post('/ghost/auth/', json.dumps({'token': self.token.token}), content_type='application/json')
        self.assertEqual(response.status_code, 200)


class ActivityTests(SecurityFixture):
    def setUp(self):
        super().setUp()
        self.security, _ = self.authorize()
        self.headerToValue = {'HTTP_X_SESSION_ID': self.security.session.session_id, 'HTTP_X_GHOST_LEASE': self.security.producer_secret}
        buffer = BytesIO()
        Image.new('RGB', (32, 20), color='white').save(buffer, format='JPEG')
        self.jpeg = buffer.getvalue()

    def consent(self, enabled=True):
        return self.post('/ghost/v2/activity/consent/', {'enabled': enabled, 'mode': 'local', 'device_name': 'Test laptop'}, **self.headerToValue)

    def screenshot(self, image=None):
        return self.client.post('/ghost/v2/activity/screenshot/', data=self.jpeg if image is None else image,
                                content_type='image/jpeg', **self.headerToValue)

    def test_default_no_upload_and_explicit_consent_in_local_mode(self):
        GhostAccessToken.objects.filter(pk=self.token.pk).update(allow_remote=False)
        self.assertEqual(self.screenshot().status_code, 403)
        self.assertEqual(self.consent().status_code, 200)
        self.assertEqual(self.screenshot().status_code, 200)
        self.assertEqual(GhostTokenScreenshot.objects.count(), 1)
        self.assertEqual(GhostTokenDevice.objects.get().label, 'Test laptop')
        self.assertEqual(self.screenshot().status_code, 429)
        GhostTokenScreenshot.objects.update(created_at=timezone.now()-timedelta(seconds=301))
        self.assertEqual(self.screenshot().status_code, 200)
        self.assertEqual(GhostTokenScreenshot.objects.count(), 2)

    def test_consent_and_admin_disabling_stop_archive(self):
        self.consent()
        self.consent(False)
        self.assertEqual(self.screenshot().status_code, 403)
        self.consent()
        GhostAccessToken.objects.filter(pk=self.token.pk).update(activity_recording_enabled=False)
        self.assertEqual(self.screenshot().status_code, 403)
        self.assertEqual(self.consent().status_code, 403)

    def test_viewer_credentials_never_upload(self):
        self.headerToValue['HTTP_X_GHOST_LEASE'] = self.security.viewer_secret
        self.assertEqual(self.consent().status_code, 401)
        self.assertEqual(self.screenshot().status_code, 401)

    def test_invalid_images_not_archived(self):
        self.consent()
        for image in (b'bad', b'\xff\xd8\xffnot-a-jpeg\xff\xd9', b'x'*(4*1024*1024+1)):
            self.assertIn(self.screenshot(image).status_code, (400, 413))
        self.assertFalse(GhostTokenScreenshot.objects.exists())

    def test_local_messages_idempotent_not_delivered_twice(self):
        value = {'event_id': str(uuid.uuid4()), 'text': 'Test <script>message</script> [answer]'}
        self.assertEqual(self.post('/ghost/v2/activity/text/', value, **self.headerToValue).status_code, 403)
        self.consent()
        for _ in range(2):
            self.assertEqual(self.post('/ghost/v2/activity/text/', value, **self.headerToValue).status_code, 200)
        self.assertEqual(GhostTextMessage.objects.count(), 1)
        self.assertTrue(GhostTextMessage.objects.get().delivered)
        value['text'] = 'changed'
        self.assertEqual(self.post('/ghost/v2/activity/text/', value, **self.headerToValue).status_code, 409)
        poll = self.client.get('/ghost/v2/text/poll/', **self.headerToValue)
        self.assertEqual(poll.json()['messages'], [])

    def test_private_admin_sections_and_image_permission(self):
        self.consent()
        self.screenshot()
        image = GhostTokenScreenshot.objects.get()
        image_url = reverse('admin:ghost_note_token_archive_image', args=[self.token.pk, image.pk])
        self.assertEqual(self.client.get(image_url).status_code, 302)
        user = get_user_model().objects.create_user('staff', password='test', is_staff=True)
        self.client.force_login(user)
        self.assertEqual(self.client.get(image_url).status_code, 403)
        user.is_superuser = True
        user.save()
        response = self.client.get(image_url)
        self.assertEqual(response.status_code, 200)
        self.assertIn('private', response['Cache-Control'])
        self.assertIn('no-store', response['Cache-Control'])
        wrong = reverse('admin:ghost_note_token_archive_image', args=[self.token.pk+99, image.pk])
        self.assertEqual(self.client.get(wrong).status_code, 404)
        response = self.client.get(reverse('admin:ghost_note_ghostaccesstoken_change', args=[self.token.pk]))
        self.assertEqual(response.status_code, 200)
        for section in ('Устройства', 'Сессии', 'Сообщения', 'Скриншоты', 'Нейросеть'):
            self.assertContains(response, section)
        self.assertNotContains(response, self.security.producer_secret)
        for model, lookup in [('ghosttextmessage', 'session__access_token__id__exact'),
                              ('ghostairequest', 'thread__access_token__id__exact'),
                              ('ghosttokenscreenshot', 'access_token__id__exact')]:
            url = reverse('admin:ghost_note_{}_changelist'.format(model))
            self.assertEqual(self.client.get(url, {lookup: self.token.pk}).status_code, 200)


    def test_cleanup_inactive_token(self):
        self.consent()
        self.screenshot()
        GhostTokenScreenshot.objects.update(created_at=timezone.now()-timedelta(days=8))
        GhostAccessToken.objects.filter(pk=self.token.pk).update(is_active=False)
        call_command('cleanup_ghost_activity', verbosity=0)
        self.assertFalse(GhostTokenScreenshot.objects.exists())


# Reuse setup/helpers without inheriting all historical scenario tests.
class AICooldownTests(AIConversationTests):
    def test_cooldown_across_devices_reset_and_failed_provider(self):
        self.assertEqual(self.chat().status_code, 200)
        response = self.chat()
        self.assertEqual(response.status_code, 429)
        self.assertIn('Подождите 30 сек, не так часто.', response.json()['message'])
        self.assertEqual(response['Retry-After'], '30')
        other = self.make_security(self.token)
        self.assertEqual(self.chat(security=other).status_code, 429)
        self.assertEqual(self.reset().status_code, 200)
        self.assertEqual(self.chat().status_code, 429)
        self.assertEqual(self.provider.call_count, 1)
        self.elapse_cooldown()
        self.provider.side_effect = RuntimeError()
        self.assertEqual(self.chat().status_code, 503)
        self.assertEqual(self.chat().status_code, 429)
        self.assertEqual(self.provider.call_count, 2)

    def test_exact_boundary_and_round_up(self):
        now = timezone.now()
        GhostAccessToken.objects.filter(pk=self.token.pk).update(last_ai_used_at=now-timedelta(seconds=29.2))
        with patch('ghost_note.ai_service.timezone.now', return_value=now):
            response = self.chat()
            self.assertEqual(response.json()['retry_after'], '1')
        GhostAccessToken.objects.filter(pk=self.token.pk).update(last_ai_used_at=now-timedelta(seconds=30))
        with patch('ghost_note.ai_service.timezone.now', return_value=now):
            self.assertEqual(self.chat().status_code, 200)

    def test_idempotent_retry_does_not_spend_or_hit_cooldown(self):
        request_id = uuid.uuid4()
        self.chat(request_id)
        self.assertTrue(self.chat(request_id).json()['duplicate'])
        self.assertEqual(self.provider.call_count, 1)
        self.assertEqual(GhostAIRequest.objects.count(), 1)

    def test_daily_request_cap_is_token_wide(self):
        GhostAccessToken.objects.filter(pk=self.token.pk).update(ai_daily_request_limit=1)
        self.chat()
        self.elapse_cooldown()
        response = self.chat(security=self.make_security(self.token))
        self.assertEqual(response.json()['error'], 'ai_daily_limit')
        self.assertEqual(self.provider.call_count, 1)


class ActivityMigrationTests(TransactionTestCase):
    def test_existing_tokens_stay_unrestricted_new_tokens_one_device(self):
        executor = MigrationExecutor(connection)
        executor.migrate([('ghost_note', '0020_ai_access_history')])
        old = executor.loader.project_state([('ghost_note', '0020_ai_access_history')]).apps.get_model('ghost_note', 'GhostAccessToken')
        old.objects.create(token='OLD-VALID', token_type='test', expires_at=timezone.now()+timedelta(hours=1))
        try:
            executor = MigrationExecutor(connection)
            executor.migrate([('ghost_note', '0021_activity_security')])
            token = GhostAccessToken.objects.get(token='OLD-VALID')
            self.assertEqual((token.max_devices, token.max_concurrent_sessions), (0, 0))
            historical_token = executor.loader.project_state([('ghost_note', '0021_activity_security')]).apps.get_model('ghost_note', 'GhostAccessToken')
            new = historical_token.objects.create(token='V21-NEW', token_type='test', expires_at=timezone.now()+timedelta(hours=1))
            self.assertEqual((new.max_devices, new.max_concurrent_sessions), (1, 1))
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
