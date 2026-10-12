from datetime import timedelta
import uuid

from django.utils import timezone

from .models import GhostAccessToken, GhostAIRequest, GhostAIThread, GhostSecuritySession, GhostSession, GhostTextMessage
from .session_links import owner_session_data, viewer_path_for_token
from .test_security_v2 import SecurityFixture


class AIViewerTests(SecurityFixture):
    def setUp(self):
        super().setUp()
        self.token.allow_local = False
        self.token.allow_remote = False
        self.token.allow_ai = True
        self.token.save(update_fields=['allow_local', 'allow_remote', 'allow_ai'])
        self.security, _lines = self.authorize()
        self.session_id = self.security.session.session_id
        self.queryToValue = {'session_id': self.session_id}
        self.producerHeaderToValue = {'HTTP_X_GHOST_LEASE': self.security.producer_secret}
        self.viewerHeaderToValue = {'HTTP_X_GHOST_VIEWER': self.security.viewer_secret}
        self.viewer_path = '/ghost/v2/viewer/{}/'.format(self.session_id)

    def viewer(self, **queryToOverride):
        queryToValue = {'key': self.security.viewer_secret}
        queryToValue.update(queryToOverride)
        return self.client.get(self.viewer_path, queryToValue)

    def send(self, text='[Текст] после скобки 😀'):
        return self.post('/ghost/v2/text/', {'session_id': self.session_id, 'text': text}, **self.viewerHeaderToValue)

    def poll(self):
        return self.client.get('/ghost/v2/text/poll/', self.queryToValue, **self.producerHeaderToValue)

    def assert_all_text_status(self, status):
        self.assertEqual(self.viewer().status_code, status)
        self.assertEqual(self.send().status_code, status)
        self.assertEqual(self.poll().status_code, status)

    def test_ai_viewer_has_screen_and_text_including_old_links_without_audio(self):
        for queryToValue in ({}, {'mode': 'text'}, {'mode': 'ai'}, {'mode': 'remote'}):
            response = self.viewer(**queryToValue)
            self.assertTemplateUsed(response, 'ghost_note/viewer_v2.html')
            self.assertContains(response, 'Текст для окна на ПК')
            self.assertContains(response, '/ghost/v2/text/')
            self.assertContains(response, '/ghost/v2/screenshot/get/')
            self.assertNotContains(response, '/ghost/v2/audio/')
            self.assertNotContains(response, 'new WebSocket')
            self.assertNotContains(response, self.token.token)
            self.assertNotContains(response, self.security.producer_secret)
            self.assertEqual(response['Cache-Control'], 'no-store')
            self.assertEqual(response['Referrer-Policy'], 'no-referrer')
            self.assertEqual(response['X-Frame-Options'], 'DENY')

    def test_text_roundtrip_preserves_all_characters_and_consumes_once(self):
        messages = ['[начало] текст после скобки', '😀 "кавычки"\\слэш\nи новая строка',
                    '</textarea><script>alert("text")</script>', r'\u005d [после]']
        for message in messages:
            self.assertEqual(self.send(message).json(), {'ok': True})
        self.assertEqual(self.poll().json(), {'messages': messages})
        self.assertEqual(self.poll().json(), {'messages': []})
        self.assertFalse(GhostTextMessage.objects.filter(delivered=False).exists())
        # Viewer messages are independent from the AI conversation and never run Qwen.
        from .models import GhostAIRequest, GhostAIThread
        self.assertFalse(GhostAIRequest.objects.exists())
        self.assertFalse(GhostAIThread.objects.exists())

    def test_ai_only_cannot_use_remote_audio(self):
        for route in ['/ghost/v2/audio/poll/']:
            response = self.client.get(route, self.queryToValue, **self.viewerHeaderToValue)
            self.assertEqual(response.status_code, 403, route)
            self.assertEqual(response.json()['error'], 'remote_not_allowed')
        response = self.client.get('/ghost/v2/audio/status/', self.queryToValue, **self.producerHeaderToValue)
        self.assertEqual(response.status_code, 403)
        for route in ['/ghost/v2/audio/on/', '/ghost/v2/audio/off/']:
            self.assertEqual(self.post(route, self.queryToValue, **self.viewerHeaderToValue).status_code, 403, route)
        for route, content_type in [('/ghost/v2/audio/upload/', 'application/octet-stream')]:
            response = self.client.post(route + '?session_id=' + self.session_id, b'fixture',
                                        content_type=content_type, **self.producerHeaderToValue)
            self.assertEqual(response.status_code, 403, route)
        self.security.session.refresh_from_db()
        self.assertFalse(self.security.session.audio_enabled)
        self.assertFalse(self.security.session.screenshot)

    def test_ai_screen_upload_roundtrip_and_roles(self):
        route = '/ghost/v2/screenshot/?session_id=' + self.session_id
        read = '/ghost/v2/screenshot/get/'
        self.assertEqual(self.client.get(read, self.queryToValue, **self.viewerHeaderToValue).status_code, 204)
        jpeg = b'\xff\xd8\xffsynthetic-screen\xff\xd9'
        self.assertEqual(self.client.post(route, jpeg, content_type='image/jpeg',
            HTTP_X_GHOST_LEASE=self.security.viewer_secret).status_code, 401)
        self.assertEqual(self.client.post(route, jpeg, content_type='image/jpeg', **self.producerHeaderToValue).status_code, 200)
        response = self.client.get(read, self.queryToValue, **self.viewerHeaderToValue)
        self.assertEqual(response.content, jpeg)
        self.assertEqual(response['X-Ghost-Screen-Source'], 'live')
        self.assertEqual(self.client.get(read, self.queryToValue,
            HTTP_X_GHOST_VIEWER=self.security.producer_secret).status_code, 401)
        GhostAccessToken.objects.filter(pk=self.token.pk).update(allow_ai=False, allow_local=True)
        self.assertEqual(self.client.get(read, self.queryToValue, **self.viewerHeaderToValue).status_code, 403)
        self.assertEqual(self.client.post(route, jpeg, content_type='image/jpeg', **self.producerHeaderToValue).status_code, 403)

    def test_old_client_fallback_is_bound_to_device_and_current_session(self):
        thread = GhostAIThread.objects.create(access_token=self.token, device_key_hash=self.security.device_key_hash)
        jpeg = b'\xff\xd8\xffai-fixture\xff\xd9'
        def add(target):
            return GhostAIRequest.objects.create(thread=target, request_id=uuid.uuid4(), conversation_id=target.conversation_id,
                image_hash='1'*64, image=jpeg)
        old = add(thread)
        GhostAIRequest.objects.filter(pk=old.pk).update(created_at=self.security.created_at-timedelta(seconds=1))
        other = GhostAIThread.objects.create(access_token=self.token, device_key_hash='f'*64)
        add(other)
        read = '/ghost/v2/screenshot/get/'
        self.assertEqual(self.client.get(read, self.queryToValue, **self.viewerHeaderToValue).status_code, 204)
        add(thread)
        response = self.client.get(read, self.queryToValue, **self.viewerHeaderToValue)
        self.assertEqual(response.content, jpeg)
        self.assertEqual(response['X-Ghost-Screen-Source'], 'ai-request')
        GhostSecuritySession.objects.filter(pk=self.security.pk).update(revoked_at=timezone.now())
        self.assertEqual(self.client.get(read, self.queryToValue, **self.viewerHeaderToValue).status_code, 401)

    def test_roles_cannot_be_crossed_and_raw_token_cannot_authorize(self):
        self.assertEqual(self.viewer(key=self.security.producer_secret).status_code, 401)
        self.assertEqual(self.viewer(key='').status_code, 401)
        self.assertEqual(self.post('/ghost/v2/text/', {'session_id': self.session_id, 'text': 'blocked'},
                                  HTTP_X_GHOST_VIEWER=self.security.producer_secret).status_code, 401)
        self.assertEqual(self.post('/ghost/v2/text/', {'session_id': self.session_id, 'text': 'blocked'},
                                  HTTP_X_ACCESS_TOKEN=self.token.token).status_code, 401)
        self.assertEqual(self.send().status_code, 200)
        for headerToValue in [{'HTTP_X_GHOST_LEASE': self.security.viewer_secret},
                              {'HTTP_X_ACCESS_TOKEN': self.token.token}, {}]:
            self.assertEqual(self.client.get('/ghost/v2/text/poll/', self.queryToValue,
                                            **headerToValue).status_code, 401)
        self.assertFalse(GhostTextMessage.objects.get().delivered)
        self.assertEqual(len(self.poll().json()['messages']), 1)

    def test_disabling_ai_revokes_text_without_consuming_pending_messages(self):
        self.assertEqual(self.send().status_code, 200)
        GhostAccessToken.objects.filter(pk=self.token.pk).update(allow_ai=False)
        self.assert_all_text_status(403)
        self.assertFalse(GhostTextMessage.objects.get().delivered)
        GhostAccessToken.objects.filter(pk=self.token.pk).update(allow_ai=True)
        self.assertEqual(len(self.poll().json()['messages']), 1)

    def test_expired_revoked_and_invalid_token_states_reject_every_text_operation(self):
        for fieldToUpdate in [{'revoked_at': timezone.now()},
                              {'lease_expires_at': timezone.now() - timedelta(seconds=1)}]:
            GhostSecuritySession.objects.filter(pk=self.security.pk).update(**fieldToUpdate)
            self.assert_all_text_status(401)
            GhostSecuritySession.objects.filter(pk=self.security.pk).update(
                revoked_at=None, lease_expires_at=self.security.lease_expires_at)
        for fieldToUpdate in [{'is_active': False}, {'expires_at': timezone.now() - timedelta(seconds=1)},
                              {'starts_at': timezone.now() + timedelta(hours=1)}]:
            GhostAccessToken.objects.filter(pk=self.token.pk).update(**fieldToUpdate)
            self.assert_all_text_status(401)
            GhostAccessToken.objects.filter(pk=self.token.pk).update(
                is_active=True, starts_at=self.token.starts_at, expires_at=self.token.expires_at)
        self.assertFalse(GhostTextMessage.objects.exists())

    def test_remote_viewer_unchanged_and_can_choose_focused_text_mode(self):
        GhostAccessToken.objects.filter(pk=self.token.pk).update(allow_remote=True, allow_ai=False)
        self.assertTemplateUsed(self.viewer(), 'ghost_note/viewer_v2.html')
        self.assertTemplateUsed(self.viewer(mode='text'), 'ghost_note/viewer_text_v2.html')
        self.assertEqual(self.send('remote message').status_code, 200)
        self.assertEqual(self.poll().json(), {'messages': ['remote message']})
        self.assertEqual(self.client.get('/ghost/v2/screenshot/get/', self.queryToValue,
                                        **self.viewerHeaderToValue).status_code, 204)
        self.token.refresh_from_db()
        self.assertNotIn('mode=text', viewer_path_for_token(self.token))

    def test_owner_links_support_ai_only_and_no_legacy_fallback_without_live_session(self):
        fieldToInfo = owner_session_data(self.token)
        self.assertEqual(fieldToInfo['session_protocol'], 2)
        self.assertEqual(fieldToInfo['session_state'], 'lease_active')
        self.assertIn('&mode=ai', fieldToInfo['viewer_url'])
        self.assertIn(self.security.viewer_secret, fieldToInfo['viewer_url'])
        self.assertTrue(fieldToInfo['sessions'][0]['active'])
        self.assertIn('&mode=ai', fieldToInfo['sessions'][0]['viewer_url'])
        GhostSecuritySession.objects.filter(pk=self.security.pk).update(revoked_at=timezone.now())
        fieldToInfo = owner_session_data(self.token)
        self.assertIsNone(viewer_path_for_token(self.token))
        self.assertIsNone(fieldToInfo['viewer_url'])
        self.assertEqual(fieldToInfo['session_state'], 'unavailable')
        self.assertFalse(fieldToInfo['sessions'][0]['active'])
        self.assertIsNone(fieldToInfo['sessions'][0]['viewer_url'])

    def test_ai_only_permissions_do_not_enable_legacy_remote_upload(self):
        legacy = self.post('/ghost/session/', {'token': self.token.token})
        self.assertEqual(legacy.status_code, 403)
        # The legacy token entry page renders before checking remote access;
        # the producer upload keeps enforcing its original remote permission.
        self.assertEqual(self.client.get('/ghost/viewer/', {'token': self.token.token}).status_code, 200)
        legacy_session = GhostSession.objects.get(access_token=self.token, security_v2__isnull=True)
        self.assertEqual(self.client.post('/ghost/screenshot/?session_id=' + legacy_session.session_id,
                                         b'fixture', content_type='image/jpeg',
                                         HTTP_X_ACCESS_TOKEN=self.token.token).status_code, 403)

    def test_local_only_defaults_cannot_use_viewer_text(self):
        GhostAccessToken.objects.filter(pk=self.token.pk).update(allow_ai=False, allow_local=True)
        self.assert_all_text_status(403)
        for token_type in ('test', 'real'):
            token = GhostAccessToken(token_type=token_type)
            self.assertFalse(token.allow_ai)

    def test_invalid_text_is_rejected_without_queueing(self):
        for value in (None, {}, [], 1):
            self.assertEqual(self.send(value).status_code, 400)
        self.assertEqual(self.send('x' * 65536).status_code, 400)
        self.assertFalse(GhostTextMessage.objects.exists())

    def test_viewer_header_works_without_key_in_url(self):
        response = self.client.get(self.viewer_path, **self.viewerHeaderToValue)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.security.viewer_secret)
