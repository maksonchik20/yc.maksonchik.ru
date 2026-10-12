from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
import secrets
import tempfile
import threading

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.db import close_old_connections
from django.test import TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone

from .models import GhostAccessToken, GhostSecuritySession
from .security_v2 import (SecurityError, challenge_response, close_security_session,
                         lease_response, raw_signature, request_payload, sha256_hex)


@skipUnlessDBFeature('has_select_for_update')
class SessionConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        key = ec.generate_private_key(ec.SECP256R1())
        filename = Path(self.directory.name)/'signing.pem'
        filename.write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        config = override_settings(GHOST_V2_SIGNING_KEY_FILE=str(filename), GHOST_V2_KEY_ID='test')
        config.enable()
        self.addCleanup(config.disable)
        self.token = GhostAccessToken.objects.create(token_type='test', allow_remote=True,
            max_devices=2, max_concurrent_sessions=1, expires_at=timezone.now()+timedelta(hours=1))
        self.first_device = ec.generate_private_key(ec.SECP256R1())
        lease_response(self.proof(self.first_device))
        self.first = GhostSecuritySession.objects.get()

    def proof(self, device, session_id=''):
        public = device.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        fieldToValue = {'token': self.token.token, 'device_public_key': public.hex(),
                        'session_id': str(session_id), 'nonce': secrets.token_hex(32)}
        fieldToValue['challenge'] = challenge_response(fieldToValue)['challenge']
        fieldToValue['signature'] = raw_signature(device, request_payload(fieldToValue['challenge'],
            fieldToValue['nonce'], self.token.token, sha256_hex(public), str(session_id)))
        return fieldToValue

    def compete(self, *operations):
        barrier = threading.Barrier(len(operations))
        def execute(operation):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                operation()
                return 'ok'
            except SecurityError as error:
                return error.code
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=len(operations)) as pool:
            return list(pool.map(execute, operations))

    def test_expired_renewal_racing_new_device_never_oversubscribes(self):
        GhostSecuritySession.objects.filter(pk=self.first.pk).update(
            lease_expires_at=timezone.now()-timedelta(seconds=1), last_seen_at=timezone.now()-timedelta(minutes=3))
        renewal = self.proof(self.first_device, self.first.session.session_id)
        admission = self.proof(ec.generate_private_key(ec.SECP256R1()))
        results = self.compete(lambda: lease_response(renewal), lambda: lease_response(admission))
        self.assertEqual(results.count('ok'), 1, results)
        self.assertEqual(GhostSecuritySession.objects.filter(revoked_at__isnull=True,
            lease_expires_at__gt=timezone.now()).count(), 1)

    def test_close_racing_renewal_always_leaves_session_revoked(self):
        renewal = self.proof(self.first_device, self.first.session.session_id)
        results = self.compete(lambda: lease_response(renewal), lambda: close_security_session(
            str(self.first.session.session_id), self.first.producer_secret))
        self.assertEqual(results[1], 'ok', results)
        self.first.refresh_from_db()
        self.assertIsNotNone(self.first.revoked_at)

    def test_duplicate_close_requests_succeed(self):
        def close():
            close_security_session(str(self.first.session.session_id), self.first.producer_secret)
        self.assertEqual(self.compete(close, close), ['ok', 'ok'])
