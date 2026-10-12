import asyncio
import base64
import builtins
import importlib.util
import json
import secrets
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from .models import GhostAccessToken, GhostLeaseChallenge, GhostSecuritySession, GhostSession, GhostTextMessage
from .security_v2 import request_payload, raw_signature, sha256_hex


class SecurityFixture(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.directory = tempfile.TemporaryDirectory(prefix='ghost-v2-test-')
        cls.signing_key = ec.generate_private_key(ec.SECP256R1())
        cls.key_file = Path(cls.directory.name, 'signing.pem')
        cls.key_file.write_bytes(cls.signing_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        cls.configuration = override_settings(GHOST_V2_SIGNING_KEY_FILE=str(cls.key_file), GHOST_V2_KEY_ID='test-p256')
        cls.configuration.enable()

    @classmethod
    def tearDownClass(cls):
        cls.configuration.disable()
        cls.directory.cleanup()
        super().tearDownClass()

    def setUp(self):
        self.token = GhostAccessToken.objects.create(
            token_type='real', payment_amount=1, token='TESTV2', max_devices=0, max_concurrent_sessions=0,
            starts_at=timezone.now() - timedelta(minutes=5), expires_at=timezone.now() + timedelta(hours=1))
        self.device = ec.generate_private_key(ec.SECP256R1())
        self.public_bytes = self.device.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

    def post(self, route, fieldToValue, **headerToValue):
        return self.client.post(route, data=json.dumps(fieldToValue), content_type='application/json', **headerToValue)

    def challenge(self, **fieldToOverride):
        fieldToValue = {'token': self.token.token, 'device_public_key': self.public_bytes.hex()}
        fieldToValue.update(fieldToOverride)
        return self.post('/ghost/v2/challenge/', fieldToValue)

    def proof(self, session_id='', **fieldToOverride):
        response = self.challenge()
        self.assertEqual(response.status_code, 200, response.content)
        fieldToValue = {
            'token': self.token.token, 'device_public_key': self.public_bytes.hex(),
            'challenge': response.json()['challenge'], 'nonce': secrets.token_hex(32), 'session_id': session_id,
        }
        fieldToValue.update(fieldToOverride)
        fieldToValue['signature'] = raw_signature(self.device, request_payload(
            fieldToValue['challenge'], fieldToValue['nonce'], fieldToValue['token'],
            sha256_hex(bytes.fromhex(fieldToValue['device_public_key'])), fieldToValue['session_id']))
        return fieldToValue

    def authorize(self, session_id=''):
        fieldToValue = self.proof(session_id)
        response = self.post('/ghost/v2/lease/', fieldToValue)
        self.assertEqual(response.status_code, 200, response.content)
        envelopeToValue = response.json()
        payload = base64.b64decode(envelopeToValue['payload'], validate=True)
        raw = bytes.fromhex(envelopeToValue['signature'])
        self.assertEqual(len(raw), 64)
        signature = utils.encode_dss_signature(int.from_bytes(raw[:32], 'big'), int.from_bytes(raw[32:], 'big'))
        self.signing_key.public_key().verify(signature, payload, ec.ECDSA(hashes.SHA256()))
        lines = payload.decode('ascii').split('\n')
        self.assertEqual(len(lines), 16)
        self.assertEqual(lines[-2:], ['END', ''])
        self.assertEqual(lines[:5], ['GHOST-LICENSE-V2', 'test-p256', fieldToValue['nonce'],
                                   sha256_hex(self.public_bytes), sha256_hex(self.token.token.encode())])
        self.assertLessEqual(int(lines[7]) - int(lines[6]), 600)
        self.assertLessEqual(int(lines[7]), int(lines[9]))
        self.assertEqual(lines[8], str(int(self.token.starts_at.timestamp())))
        self.assertEqual(lines[9], str(int(self.token.expires_at.timestamp())))
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(response['Referrer-Policy'], 'no-referrer')
        return GhostSecuritySession.objects.get(session__session_id=lines[5]), lines


class SecurityV2Tests(SecurityFixture):
    def test_signed_contract_new_session_and_renewal(self):
        first, lines = self.authorize()
        renewed, new_lines = self.authorize(first.session.session_id)
        self.assertEqual(first.pk, renewed.pk)
        self.assertEqual(lines[12:14], new_lines[12:14])
        self.assertEqual(lines[10:12], ['1', '1'])
        self.assertEqual(GhostSession.objects.count(), 1)
        self.assertEqual(GhostLeaseChallenge.objects.filter(consumed_at__isnull=False).count(), 2)

    def test_device_proof_cannot_be_replayed(self):
        fieldToValue = self.proof()
        self.assertEqual(self.post('/ghost/v2/lease/', fieldToValue).status_code, 200)
        self.assertEqual(self.post('/ghost/v2/lease/', fieldToValue).status_code, 401)
        self.assertEqual(GhostSecuritySession.objects.count(), 1)

    def test_bad_signature_does_not_consume_challenge(self):
        fieldToValue = self.proof()
        signature = fieldToValue['signature']
        fieldToValue['signature'] = '00' * 64
        self.assertEqual(self.post('/ghost/v2/lease/', fieldToValue).status_code, 401)
        self.assertIsNone(GhostLeaseChallenge.objects.get().consumed_at)
        fieldToValue['signature'] = signature
        self.assertEqual(self.post('/ghost/v2/lease/', fieldToValue).status_code, 200)

    def test_changed_nonce_or_session_invalidates_proof(self):
        for field, value in [('nonce', '11' * 32), ('session_id', str(uuid.uuid4()))]:
            fieldToValue = self.proof()
            fieldToValue[field] = value
            self.assertEqual(self.post('/ghost/v2/lease/', fieldToValue).status_code, 401)
        self.assertFalse(GhostSecuritySession.objects.exists())

    def test_expired_challenge_rejected(self):
        fieldToValue = self.proof()
        GhostLeaseChallenge.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.post('/ghost/v2/lease/', fieldToValue).status_code, 401)

    def test_challenge_is_bound_to_device_and_token(self):
        fieldToValue = self.proof()
        other_device = ec.generate_private_key(ec.SECP256R1())
        other_public = other_device.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        fieldToValue['device_public_key'] = other_public.hex()
        fieldToValue['signature'] = raw_signature(other_device, request_payload(
            fieldToValue['challenge'], fieldToValue['nonce'], self.token.token, sha256_hex(other_public), ''))
        self.assertEqual(self.post('/ghost/v2/lease/', fieldToValue).status_code, 401)
        self.assertIsNone(GhostLeaseChallenge.objects.get().consumed_at)

    def test_license_state_is_checked_on_both_challenge_and_lease(self):
        for fieldToUpdate, error in [({'is_active': False}, 'invalid'),
                                    ({'starts_at': timezone.now() + timedelta(hours=1)}, 'not_started'),
                                    ({'expires_at': timezone.now() - timedelta(seconds=1)}, 'expired')]:
            fieldToValue = self.proof()
            GhostAccessToken.objects.filter(pk=self.token.pk).update(**fieldToUpdate)
            self.assertEqual(self.challenge().json()['error'], error)
            self.assertEqual(self.post('/ghost/v2/lease/', fieldToValue).json()['error'], error)
            GhostAccessToken.objects.filter(pk=self.token.pk).update(
                is_active=True, starts_at=self.token.starts_at, expires_at=self.token.expires_at)

    def test_lease_is_capped_by_license_expiration(self):
        self.token.expires_at = timezone.now() + timedelta(seconds=37)
        self.token.save(update_fields=['expires_at'])
        _security, lines = self.authorize()
        self.assertEqual(lines[7], lines[9])

    def test_missing_key_disables_only_v2(self):
        with override_settings(GHOST_V2_SIGNING_KEY_FILE='/nonexistent/key.pem'):
            self.assertEqual(self.challenge().status_code, 503)
            response = self.post('/ghost/auth/', {'token': self.token.token})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(set(response.json()), {'ok', 'starts_at', 'expires_at', 'allow_local', 'allow_remote'})

    def test_json_key_configuration_and_invalid_key_id(self):
        config = Path(self.directory.name, 'security-v2.json')
        config.write_text(json.dumps({'signing_key_file': str(self.key_file), 'key_id': 'fallback-key'}))
        with override_settings(GHOST_V2_SIGNING_KEY_FILE='', GHOST_V2_KEY_ID='', GHOST_V2_CONFIG_FILE=str(config)):
            self.assertEqual(self.challenge().status_code, 200)
            config.write_text('{invalid json')
            self.assertEqual(self.challenge().status_code, 503)
        with override_settings(GHOST_V2_KEY_ID='invalid.key'):
            self.assertEqual(self.challenge().status_code, 503)

    def test_missing_crypto_dependency_never_breaks_v1(self):
        original_import = builtins.__import__

        def without_crypto(name, *args, **keywordToValue):
            if name.startswith('cryptography'):
                raise ImportError('isolated missing optional dependency')
            return original_import(name, *args, **keywordToValue)

        with patch('builtins.__import__', side_effect=without_crypto):
            self.assertEqual(self.post('/ghost/auth/', {'token': self.token.token}).status_code, 200)
            self.assertEqual(self.challenge().status_code, 503)

    def test_owner_details_choose_v2_without_changing_public_lists(self):
        from . import panel_data
        from .session_links import viewer_path_for_token
        from .auth import get_or_create_session_for_token
        legacy = get_or_create_session_for_token(self.token)
        self.assertIn('/ghost/viewer/?token=', viewer_path_for_token(self.token))
        security, _lines = self.authorize()
        fieldToDetail = panel_data.token_detail(self.token)
        self.assertEqual(fieldToDetail['session_protocol'], 2)
        self.assertEqual(fieldToDetail['session_id'], security.session.session_id)
        self.assertIn('/ghost/v2/viewer/', fieldToDetail['viewer_url'])
        self.assertIn(security.viewer_secret, fieldToDetail['viewer_url'])
        self.assertNotIn(security.producer_secret, json.dumps(fieldToDetail))
        self.assertEqual({item['id'] for item in fieldToDetail['sessions']}, {legacy.session_id, security.session.session_id})
        listing = json.dumps(panel_data.token_list({}))
        self.assertNotIn(security.producer_secret, listing)
        self.assertNotIn(security.viewer_secret, listing)
        self.assertNotIn(self.token.token, listing)
        GhostSecuritySession.objects.filter(pk=security.pk).update(revoked_at=timezone.now())
        self.assertIn('/ghost/viewer/?token=', viewer_path_for_token(self.token))

    def test_v1_remote_roundtrip_remains_unchanged_alongside_v2(self):
        security, _lines = self.authorize()
        response = self.post('/ghost/session/', {'token': self.token.token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {'session_id', 'viewer_url'})
        session_id = response.json()['session_id']
        self.assertNotEqual(session_id, security.session.session_id)
        self.assertEqual(self.client.post('/ghost/screenshot/?session_id=' + session_id, b'legacy-jpeg',
                                         content_type='image/jpeg', HTTP_X_ACCESS_TOKEN=self.token.token).status_code, 200)
        self.assertEqual(self.client.get('/ghost/screenshot/get/', {'session_id': session_id}).content, b'legacy-jpeg')
        self.assertEqual(self.post('/ghost/audio/on/', {'session_id': session_id}).status_code, 200)
        self.assertEqual(self.client.get('/ghost/audio/status/', {'session_id': session_id},
                                        HTTP_X_ACCESS_TOKEN=self.token.token).json(), {'enabled': True})
        self.assertEqual(self.post('/ghost/audio/off/', {'session_id': session_id}).status_code, 200)

    def test_strict_input_types_and_body_limits(self):
        for value in [None, [], {}, 4, ' ']:
            self.assertEqual(self.challenge(token=value).status_code, 401)
        for value in ['', '04' + '00' * 64, '03' + '00' * 32, [], None]:
            self.assertEqual(self.challenge(device_public_key=value).status_code, 400)
        self.assertEqual(self.post('/ghost/v2/challenge/', []).status_code, 400)
        self.assertEqual(self.post('/ghost/v2/challenge/', {'padding': 'x' * 9000}).status_code, 400)
        self.assertEqual(self.client.get('/ghost/v2/lease/').status_code, 405)

    def test_producer_and_viewer_have_separate_capabilities(self):
        security, _lines = self.authorize()
        session_id = security.session.session_id
        producerHeaderToValue = {'HTTP_X_GHOST_LEASE': security.producer_secret}
        viewerHeaderToValue = {'HTTP_X_GHOST_VIEWER': security.viewer_secret}
        queryToValue = {'session_id': session_id}
        self.assertEqual(self.client.post('/ghost/v2/screenshot/?session_id=' + session_id,
                                         b'jpeg', content_type='image/jpeg', **producerHeaderToValue).status_code, 200)
        response = self.client.get('/ghost/v2/screenshot/get/', queryToValue, **viewerHeaderToValue)
        self.assertEqual(response.content, b'jpeg')
        self.assertEqual(self.client.get('/ghost/v2/screenshot/get/', queryToValue,
                                        HTTP_X_GHOST_VIEWER=security.producer_secret).status_code, 401)
        self.assertEqual(self.client.get('/ghost/v2/text/poll/', queryToValue,
                                        HTTP_X_GHOST_LEASE=security.viewer_secret).status_code, 401)
        self.assertEqual(self.client.get('/ghost/v2/text/poll/', queryToValue,
                                        HTTP_X_ACCESS_TOKEN=self.token.token).status_code, 401)
        text = 'Скобки [1] продолжаются 😀'
        self.assertEqual(self.post('/ghost/v2/text/', {'session_id': session_id, 'text': text}, **viewerHeaderToValue).status_code, 200)
        self.assertEqual(self.client.get('/ghost/v2/text/poll/', queryToValue, **producerHeaderToValue).json(), {'messages': [text]})
        viewer = self.client.get('/ghost/v2/viewer/{}/'.format(session_id), {'key': security.viewer_secret})
        self.assertEqual(viewer.status_code, 200)
        self.assertNotContains(viewer, security.producer_secret)
        self.assertNotContains(viewer, self.token.token)
        self.assertContains(viewer, 'protocol=2')
        self.assertContains(viewer, 'X-Ghost-Viewer')

    def test_remote_scope_is_enforced_even_for_a_valid_local_lease(self):
        GhostAccessToken.objects.filter(pk=self.token.pk).update(allow_remote=False)
        security, lines = self.authorize()
        self.assertEqual(lines[10:12], ['1', '0'])
        self.assertEqual(self.client.get('/ghost/v2/audio/status/', {'session_id': security.session.session_id},
                                        HTTP_X_GHOST_LEASE=security.producer_secret).status_code, 403)

    def test_v2_session_not_usable_with_any_legacy_endpoint(self):
        security, _lines = self.authorize()
        session_id = security.session.session_id
        for route in ['/ghost/screenshot/get/', '/ghost/text/poll/', '/ghost/audio/status/', '/ghost/audio/poll/']:
            self.assertEqual(self.client.get(route, {'session_id': session_id}, HTTP_X_ACCESS_TOKEN=self.token.token).status_code, 401)
        for route in ['/ghost/text/', '/ghost/audio/on/', '/ghost/audio/off/']:
            self.assertEqual(self.post(route, {'session_id': session_id, 'text': 'test'}, HTTP_X_ACCESS_TOKEN=self.token.token).status_code, 401)
        self.assertEqual(self.client.get('/ghost/viewer/{}/'.format(session_id)).status_code, 401)
        self.assertEqual(self.client.post('/ghost/screenshot/?session_id=' + session_id, b'jpeg',
                                         content_type='image/jpeg', HTTP_X_ACCESS_TOKEN=self.token.token).status_code, 400)
        self.assertEqual(self.client.post('/ghost/audio/upload/?session_id=' + session_id, b'pcm',
                                         content_type='application/octet-stream', HTTP_X_ACCESS_TOKEN=self.token.token).status_code, 401)
        self.assertFalse(GhostTextMessage.objects.exists())

    def test_v1_session_cannot_be_upgraded_or_accessed_as_v2(self):
        legacy = GhostSession.objects.create(access_token=self.token)
        fieldToValue = self.proof(legacy.session_id)
        self.assertEqual(self.post('/ghost/v2/lease/', fieldToValue).status_code, 401)
        self.assertEqual(self.client.get('/ghost/v2/screenshot/get/', {'session_id': legacy.session_id},
                                        HTTP_X_GHOST_VIEWER='00' * 32).status_code, 401)
        self.assertFalse(GhostSecuritySession.objects.exists())
        self.assertEqual(self.client.get('/ghost/viewer/{}/'.format(legacy.session_id)).status_code, 200)

    def test_revocation_and_expiry_are_checked_every_operation(self):
        security, _lines = self.authorize()
        queryToValue = {'session_id': security.session.session_id}
        for fieldToUpdate in [{'revoked_at': timezone.now()}, {'lease_expires_at': timezone.now() - timedelta(seconds=1)}]:
            GhostSecuritySession.objects.filter(pk=security.pk).update(**fieldToUpdate)
            self.assertEqual(self.client.get('/ghost/v2/screenshot/get/', queryToValue,
                                            HTTP_X_GHOST_VIEWER=security.viewer_secret).status_code, 401)
            self.assertEqual(self.client.get('/ghost/v2/audio/status/', queryToValue,
                                            HTTP_X_GHOST_LEASE=security.producer_secret).status_code, 401)
            GhostSecuritySession.objects.filter(pk=security.pk).update(revoked_at=None, lease_expires_at=security.lease_expires_at)
        GhostAccessToken.objects.filter(pk=self.token.pk).update(is_active=False)
        self.assertEqual(self.client.get('/ghost/v2/audio/status/', queryToValue,
                                        HTTP_X_GHOST_LEASE=security.producer_secret).status_code, 401)

    def test_deleted_owner_fails_closed_and_legacy_cannot_claim_session(self):
        security, _lines = self.authorize()
        session_id = security.session.session_id
        self.token.delete()
        self.assertTrue(GhostSecuritySession.objects.filter(pk=security.pk).exists())
        self.assertEqual(self.client.get('/ghost/v2/screenshot/get/', {'session_id': session_id},
                                        HTTP_X_GHOST_VIEWER=security.viewer_secret).status_code, 401)
        self.assertEqual(self.client.get('/ghost/viewer/{}/'.format(session_id)).status_code, 401)
        replacement = GhostAccessToken.objects.create(token_type='real', payment_amount=1, expires_at=timezone.now() + timedelta(hours=1))
        self.assertEqual(self.client.post('/ghost/screenshot/?session_id=' + session_id, b'jpeg',
                                         content_type='image/jpeg', HTTP_X_ACCESS_TOKEN=replacement.token).status_code, 400)
        security.session.refresh_from_db()
        self.assertIsNone(security.session.access_token_id)

    def test_different_device_cannot_renew_existing_session(self):
        security, _lines = self.authorize()
        self.device = ec.generate_private_key(ec.SECP256R1())
        self.public_bytes = self.device.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.assertEqual(self.post('/ghost/v2/lease/', self.proof(security.session.session_id)).status_code, 401)

    def test_audio_http_uses_same_lease_and_viewer_policy(self):
        security, _lines = self.authorize()
        queryToValue = {'session_id': security.session.session_id}
        viewerHeaderToValue = {'HTTP_X_GHOST_VIEWER': security.viewer_secret}
        producerHeaderToValue = {'HTTP_X_GHOST_LEASE': security.producer_secret}
        self.assertEqual(self.post('/ghost/v2/audio/on/', queryToValue, **viewerHeaderToValue).status_code, 200)
        self.assertEqual(self.client.get('/ghost/v2/audio/status/', queryToValue, **producerHeaderToValue).json(), {'enabled': True})
        self.assertEqual(self.client.post('/ghost/v2/audio/upload/?session_id=' + security.session.session_id,
                                         b'pcm', content_type='application/octet-stream', **producerHeaderToValue).status_code, 200)
        self.assertEqual(self.client.get('/ghost/v2/audio/poll/', queryToValue, **viewerHeaderToValue).content, b'pcm')
        self.assertEqual(self.post('/ghost/v2/audio/off/', queryToValue, **viewerHeaderToValue).status_code, 200)


def load_relay():
    path = Path(__file__).resolve().parent.parent / 'scripts' / 'ghost_audio_relay.py'
    spec = importlib.util.spec_from_file_location('ghost_relay_test_v2', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RelayV2Tests(SecurityFixture):
    def test_relay_enforces_protocol_and_separate_credentials(self):
        relay = load_relay()
        security, _lines = self.authorize()
        GhostSession.objects.filter(pk=security.session_id).update(audio_enabled=True)
        session_id = security.session.session_id
        self.assertEqual(relay._validate_uploader(session_id, self.token.token)[1], 'protocol mismatch')
        self.assertEqual(relay._validate_listener(session_id)[1], 'protocol mismatch')
        self.assertIsNone(relay._validate_uploader(session_id, '', '2', security.producer_secret)[1])
        self.assertIsNone(relay._validate_listener(session_id, '2', security.viewer_secret)[1])
        self.assertEqual(relay._validate_listener(session_id, '2', security.producer_secret)[1], 'unauthorized')
        self.assertEqual(relay._validate_uploader(session_id, self.token.token, '2', '')[1], 'unauthorized')
        GhostAccessToken.objects.filter(pk=self.token.pk).update(is_active=False)
        self.assertEqual(relay._validate_uploader(session_id, '', '2', security.producer_secret)[1], 'unauthorized')


class RelayIdleTests(SimpleTestCase):
    def test_room_cleanup_does_not_remove_replacement_source(self):
        relay = load_relay()
        room = relay.get_room('first')
        room.source = object()
        relay.discard_empty_room('first', room)
        self.assertIs(relay.sessionIdToRoom['first'], room)
        room.source = None
        relay.discard_empty_room('first', room)
        self.assertNotIn('first', relay.sessionIdToRoom)

    def test_idle_listener_rechecks_authorization_and_closes(self):
        relay = load_relay()

        async def scenario():
            class Socket:
                request_headers = {}

                def __init__(self):
                    self.closed = asyncio.Event()
                    self.code = None

                async def wait_closed(self):
                    await self.closed.wait()

                async def close(self, code, reason):
                    self.code = code
                    self.closed.set()

            socket = Socket()
            validator = AsyncMock(side_effect=[(object(), None), (None, 'session expired')])
            with patch.object(relay, 'validate_listener', validator), patch.object(relay, 'AUTH_RECHECK_SECONDS', 0.001):
                await asyncio.wait_for(relay.handler(socket, '/?session_id=' + str(uuid.uuid4()) + '&role=listen'), timeout=1)
            self.assertEqual(socket.code, 1008)
            self.assertEqual(validator.await_count, 2)
            self.assertEqual(relay.sessionIdToRoom, {})

        asyncio.run(scenario())
