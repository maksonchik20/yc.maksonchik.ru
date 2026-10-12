import base64
import secrets
import sys
import types
import uuid
from datetime import timedelta
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from django.test import TestCase, TransactionTestCase, override_settings
from django.db import connection, models
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from . import ai_service, panel_api, panel_data
from .models import GhostAccessToken, GhostAIRequest, GhostAIResetRequest, GhostAIThread, GhostSecuritySession, GhostSession, GhostUser
from .security_v2 import raw_signature, request_payload, sha256_hex
from .test_security_v2 import SecurityFixture

JPEG = b'\xff\xd8\xff\xe0synthetic-test-image\xff\xd9'


class SignedAIPermissionTests(SecurityFixture):
    def test_signed_v3_flag_and_unchanged_v2(self):
        for enabled in (False, True):
            GhostAccessToken.objects.filter(pk=self.token.pk).update(allow_ai=enabled)
            values = self.proof()
            values['license_version'] = 3
            values['signature'] = raw_signature(self.device, request_payload(values['challenge'],
                values['nonce'], self.token.token, sha256_hex(self.public_bytes), '', license_version=3))
            response = self.post('/ghost/v2/lease/', values)
            self.assertEqual(response.status_code, 200, response.content)
            payload = base64.b64decode(response.json()['payload'])
            lines = payload.decode().split('\n')
            self.assertEqual(len(lines), 17)
            self.assertEqual(lines[0], 'GHOST-LICENSE-V3')
            self.assertEqual(lines[10:13], ['1', '1', str(int(enabled))])
            self.assertEqual(lines[-2:], ['END', ''])
            raw = bytes.fromhex(response.json()['signature'])
            sig = utils.encode_dss_signature(int.from_bytes(raw[:32], 'big'), int.from_bytes(raw[32:], 'big'))
            self.signing_key.public_key().verify(sig, payload, ec.ECDSA(hashes.SHA256()))
            security, old_lines = self.authorize()
            self.assertEqual(len(old_lines), 16)
            self.assertEqual(old_lines[12], security.producer_secret)

    def test_version_tampering_and_malformed_versions_rejected(self):
        values = self.proof()
        values['license_version'] = 3
        self.assertEqual(self.post('/ghost/v2/lease/', values).status_code, 401)
        for version in ('3', True, 4, None):
            values = self.proof()
            values['license_version'] = version
            self.assertEqual(self.post('/ghost/v2/lease/', values).status_code, 400)


class AIConversationTests(TestCase):
    def setUp(self):
        self.token = GhostAccessToken.objects.create(token_type='test', allow_local=False,
            allow_remote=False, allow_ai=True, starts_at=timezone.now()-timedelta(minutes=1),
            expires_at=timezone.now()+timedelta(hours=1))
        self.security = self.make_security(self.token)
        self.provider = Mock(return_value='Ответ: 42.')
        self.modules = patch.dict(sys.modules, {'ghost_note.ai_provider': types.SimpleNamespace(complete=self.provider)})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def make_security(self, token, device=None):
        return GhostSecuritySession.objects.create(session=GhostSession.objects.create(access_token=token),
            access_token=token, device_key_hash=device or secrets.token_hex(32), producer_secret=secrets.token_hex(32),
            viewer_secret=secrets.token_hex(32), lease_expires_at=timezone.now()+timedelta(minutes=10))

    def headers(self, security=None):
        security = security or self.security
        return {'HTTP_X_SESSION_ID': str(security.session.session_id), 'HTTP_X_GHOST_LEASE': security.producer_secret}

    def chat(self, key=None, image=JPEG, security=None):
        return self.client.post('/ghost/v2/ai/chat/', image, content_type='image/jpeg',
            HTTP_X_REQUEST_ID=str(key or uuid.uuid4()), **self.headers(security))

    def history(self, security=None):
        return self.client.get('/ghost/v2/ai/history/', **self.headers(security))

    def reset(self, key=None):
        return self.client.post('/ghost/v2/ai/reset/', data=b'', content_type='application/json',
            HTTP_X_REQUEST_ID=str(key or uuid.uuid4()), **self.headers())

    def test_ai_only_works_remote_denied_and_private_history_persists(self):
        self.assertEqual(self.chat().status_code, 200)
        self.assertEqual(self.client.get('/ghost/v2/text/poll/', **self.headers()).json(), {'messages': []})
        remote = self.client.get('/ghost/v2/audio/status/', **self.headers())
        self.assertEqual(remote.status_code, 403)
        new_session = self.make_security(self.token, self.security.device_key_hash)
        self.assertEqual(self.history(new_session).json()['answer'], 'Ответ: 42.')
        self.assertEqual(len(self.history().json()['messages']), 2)
        self.assertEqual(self.history(self.make_security(self.token)).json()['messages'], [])
        other = GhostAccessToken.objects.create(token_type='test', allow_ai=True,
            expires_at=timezone.now()+timedelta(hours=1))
        self.assertEqual(self.history(self.make_security(other, self.security.device_key_hash)).json()['messages'], [])

    def test_permission_off_and_viewer_never_call_provider(self):
        GhostAccessToken.objects.filter(pk=self.token.pk).update(allow_ai=False)
        for response in (self.chat(), self.history(), self.reset()):
            self.assertEqual(response.status_code, 403)
        GhostAccessToken.objects.filter(pk=self.token.pk).update(allow_ai=True)
        response = self.client.post('/ghost/v2/ai/chat/', JPEG, content_type='image/jpeg',
            HTTP_X_REQUEST_ID=str(uuid.uuid4()), HTTP_X_SESSION_ID=str(self.security.session.session_id),
            HTTP_X_GHOST_LEASE=self.security.viewer_secret)
        self.assertEqual(response.status_code, 401)
        self.provider.assert_not_called()

    def elapse_cooldown(self):
        GhostAccessToken.objects.filter(pk=self.token.pk).update(last_ai_used_at=timezone.now()-timedelta(seconds=31))

    def test_idempotency_conflict_and_reset_history_archive(self):
        key = uuid.uuid4()
        self.assertEqual(self.chat(key).status_code, 200)
        self.assertTrue(self.chat(key).json()['duplicate'])
        self.assertEqual(self.provider.call_count, 1)
        self.assertEqual(self.chat(key, JPEG.replace(b'synthetic', b'changed')).json()['error'], 'ai_request_conflict')
        reset_key = uuid.uuid4()
        first = self.reset(reset_key)
        self.assertEqual(first.status_code, 200)
        self.elapse_cooldown()
        self.chat()
        second = self.reset(reset_key)
        self.assertEqual(second.json()['conversation_id'], first.json()['conversation_id'])
        self.assertTrue(second.json()['duplicate'])
        self.assertEqual(len(self.history().json()['messages']), 2)
        self.assertEqual(GhostAIRequest.objects.count(), 2)
        self.assertEqual(self.chat(key).json()['error'], 'ai_conversation_reset')

    def test_disable_revoke_and_expire_during_provider_never_release_answer(self):
        for kind in ('permission', 'token', 'lease', 'session'):
            GhostAccessToken.objects.filter(pk=self.token.pk).update(allow_ai=True, is_active=True)
            GhostSecuritySession.objects.filter(pk=self.security.pk).update(revoked_at=None,
                lease_expires_at=timezone.now()+timedelta(minutes=10))
            def during_call(messages):
                if kind == 'permission':
                    GhostAccessToken.objects.filter(pk=self.token.pk).update(allow_ai=False)
                elif kind == 'token':
                    GhostAccessToken.objects.filter(pk=self.token.pk).update(is_active=False)
                elif kind == 'lease':
                    GhostSecuritySession.objects.filter(pk=self.security.pk).update(lease_expires_at=timezone.now())
                else:
                    GhostSecuritySession.objects.filter(pk=self.security.pk).update(revoked_at=timezone.now())
                return 'Sensitive late answer'
            self.provider.side_effect = during_call
            self.elapse_cooldown()
            response = self.chat()
            self.assertIn(response.status_code, (401, 403), response.content)
            self.assertNotIn(b'Sensitive', response.content)
        self.assertFalse(GhostAIRequest.objects.filter(status='succeeded').exists())
        self.assertFalse(GhostAIThread.objects.exclude(busy_request=None).exists())

    def test_busy_rejects_concurrent_chat_and_reset(self):
        def during_call(messages):
            self.assertEqual(self.chat().json()['error'], 'ai_cooldown')
            self.assertEqual(self.reset().json()['error'], 'ai_busy')
            return 'Done'
        self.provider.side_effect = during_call
        self.assertEqual(self.chat().status_code, 200)
        self.assertEqual(self.provider.call_count, 1)

    def test_provider_failure_safe_and_claim_released(self):
        self.provider.side_effect = RuntimeError('secret-key-private-content')
        key = uuid.uuid4()
        response = self.chat(key)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b'secret', response.content)
        self.chat(key)
        self.assertEqual(self.provider.call_count, 1)
        self.assertIsNone(GhostAIThread.objects.get().busy_request)
        self.provider.side_effect = None
        self.elapse_cooldown()
        self.assertEqual(self.chat().status_code, 200)

    def test_stale_claim_late_response_discarded(self):
        def during_call(messages):
            GhostAIThread.objects.update(busy_since=timezone.now()-timedelta(seconds=121))
            return 'Too late'
        self.provider.side_effect = during_call
        self.assertEqual(self.chat().json()['error'], 'ai_timeout')
        self.assertFalse(GhostAIRequest.objects.filter(status='succeeded').exists())
        self.provider.side_effect = None
        self.elapse_cooldown()
        self.assertEqual(self.chat().status_code, 200)

    def test_malformed_or_oversize_rejected(self):
        for image in (b'', b'bad', b'\xff\xd8\xffbad', b'x'*(ai_service.MAX_IMAGE_BYTES+1)):
            self.assertIn(self.chat(image=image).status_code, (400, 413, 415))
        self.provider.assert_not_called()

    @override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=1024)
    def test_bounded_reader_avoids_django_form_limit(self):
        self.assertEqual(self.chat(image=b'\xff\xd8\xff'+b'x'*2048+b'\xff\xd9').status_code, 200)

    def test_history_payload_bound_counts_utf8_and_json_escaping(self):
        for answer in ('🙂'*16000, '\x01'*16000):
            self.reset()
            self.provider.return_value = answer
            for _ in range(3):
                self.elapse_cooldown()
                self.assertEqual(self.chat().status_code, 200)
            response = self.history()
            self.assertEqual(response.status_code, 200)
            self.assertLessEqual(len(response.content), ai_service.HISTORY_RESPONSE_BYTES)
            self.assertEqual(response.json()['answer'], answer)
            self.assertEqual(response.json()['messages'][-1]['content'], answer)
        self.assertEqual(GhostAIRequest.objects.count(), 6)

    def test_context_keeps_text_but_bounds_prior_images_and_turns(self):
        for _ in range(22):
            self.elapse_cooldown()
            self.assertEqual(self.chat().status_code, 200)
        messages = self.provider.call_args[0][0]
        self.assertEqual(len(messages), 41)
        images = [part for msg in messages if isinstance(msg['content'], list)
                  for part in msg['content'] if part['type'] == 'image_url']
        self.assertEqual(len(images), 4)
        self.assertEqual(GhostAIRequest.objects.count(), 22)
        self.assertEqual(len(self.history().json()['messages']), 44)
        previous = list(GhostAIRequest.objects.all().order_by('pk')[:5])
        for item in previous:
            item.image = b'x'*(4*1024*1024)
            item.answer = 'x'*16000
        bounded = ai_service._provider_messages(previous, b'x'*(8*1024*1024))
        images = [part for msg in bounded if isinstance(msg['content'], list)
                  for part in msg['content'] if part['type'] == 'image_url']
        self.assertEqual(len(images), 2)
        self.assertLessEqual(sum(len(msg['content']) for msg in bounded if msg['role']=='assistant'), 24000)


class AIOwnerControlTests(TestCase):
    def setUp(self):
        self.user = GhostUser.objects.create(name='AI Test Owner')

    def create(self, **changes):
        values = dict(request_id=str(uuid.uuid4()), user_id=self.user.pk, token_type='test',
            allow_local=True, allow_remote=True)
        values.update(changes)
        return panel_api.create_token(values)

    def update_payload(self, token, **changes):
        values = dict(id=token.pk, revision=panel_data.token_revision(token), label=token.label,
            starts_at=token.starts_at.isoformat(), expires_at=token.expires_at.isoformat(),
            is_active=True, allow_local=token.allow_local, allow_remote=token.allow_remote)
        values.update(changes)
        return values

    def test_defaults_off_and_manual_ai_only_and_old_update_preserves(self):
        for kind in ('test', 'real'):
            token = GhostAccessToken.objects.create(token_type=kind, payment_amount=10,
                expires_at=timezone.now()+timedelta(hours=1))
            self.assertFalse(token.allow_ai)
        result, status = self.create()
        self.assertEqual(status, 201)
        self.assertFalse(GhostAccessToken.objects.get(pk=result['id']).allow_ai)
        result, status = self.create(allow_local=False, allow_remote=False, allow_ai=True)
        self.assertEqual(status, 201, result)
        token = GhostAccessToken.objects.get(pk=result['id'])
        response, status = panel_api.update_token(self.update_payload(token, label='changed'))
        self.assertEqual(status, 200, response)
        token.refresh_from_db()
        self.assertTrue(token.allow_ai)
        self.assertTrue(response['allow_ai'])

    def test_malformed_boolean_and_revision_conflict(self):
        for value in ('true', 1, None):
            with self.assertRaises(ValueError):
                self.create(allow_ai=value)
        result, _status = self.create()
        token = GhostAccessToken.objects.get(pk=result['id'])
        for value in ('false', 0, None):
            with self.assertRaises(ValueError):
                panel_api.update_token(self.update_payload(token, allow_ai=value))
        values = self.update_payload(token)
        GhostAccessToken.objects.filter(pk=token.pk).update(allow_ai=True)
        self.assertEqual(panel_api.update_token(values)[1], 409)

    def test_omitted_and_explicit_false_creation_retry_are_equivalent(self):
        key = str(uuid.uuid4())
        first, status = self.create(request_id=key)
        self.assertEqual(status, 201)
        again, status = self.create(request_id=key, allow_ai=False)
        self.assertEqual(status, 200)
        self.assertEqual(first, again)
        self.assertEqual(GhostAccessToken.objects.count(), 1)


class AIMigrationDefaultsTests(TransactionTestCase):
    def test_preexisting_test_and_real_tokens_remain_ai_disabled(self):
        executor = MigrationExecutor(connection)
        if connection.vendor == 'mysql':
            # Historical 0020 has no MySQL rollback workaround. Restore its
            # implicit FK indexes only in this temporary test database before
            # constructing a pre-AI fixture; leave shipped 0020 unchanged.
            with connection.schema_editor() as editor:
                for model, field_name in ((GhostAIRequest, 'thread'), (GhostAIResetRequest, 'thread'),
                                          (GhostAIThread, 'access_token')):
                    field = model._meta.get_field(field_name)
                    with connection.cursor() as cursor:
                        nameToConstraint = connection.introspection.get_constraints(cursor, model._meta.db_table)
                    if not any(value['columns'] == [field.column] and (value['index'] or value['unique'])
                               for value in nameToConstraint.values()):
                        editor.add_index(model, models.Index(fields=[field_name], name='fixture_' + model._meta.model_name + '_fk'))
        executor.migrate([('ghost_note', '0019_security_v2')])
        old_apps = executor.loader.project_state([('ghost_note', '0019_security_v2')]).apps
        old_token = old_apps.get_model('ghost_note', 'GhostAccessToken')
        for kind in ('test', 'real'):
            old_token.objects.create(token='legacy-' + kind, token_type=kind, payment_amount=10,
                expires_at=timezone.now()+timedelta(hours=1))
        executor = MigrationExecutor(connection)
        executor.migrate([('ghost_note', '0021_activity_security')])
        self.assertEqual(GhostAccessToken.objects.count(), 2)
        self.assertFalse(GhostAccessToken.objects.filter(allow_ai=True).exists())
