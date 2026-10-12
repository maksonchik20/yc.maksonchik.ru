"""Run against an isolated MySQL database; SQLite has no SELECT FOR UPDATE."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
import secrets
import tempfile
import threading
import uuid
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone

from . import ai_service, security_v2
from .models import GhostAccessToken, GhostAIRequest, GhostSecuritySession, GhostSession, GhostTokenDevice


@skipUnlessDBFeature('has_select_for_update')
class ActivityConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.token = GhostAccessToken.objects.create(token_type='test', allow_ai=True,
            expires_at=timezone.now()+timedelta(hours=1))

    def test_simultaneous_devices_cannot_take_one_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            key = ec.generate_private_key(ec.SECP256R1())
            filename = Path(directory)/'test.pem'
            filename.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            with override_settings(GHOST_V2_SIGNING_KEY_FILE=str(filename), GHOST_V2_KEY_ID='concurrency-test'):
                requests = []
                for _ in range(4):
                    device = ec.generate_private_key(ec.SECP256R1())
                    public = device.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
                    fieldToValue = {'token': self.token.token, 'device_public_key': public.hex(), 'session_id': '', 'nonce': secrets.token_hex(32)}
                    fieldToValue['challenge'] = security_v2.challenge_response(fieldToValue)['challenge']
                    fieldToValue['signature'] = security_v2.raw_signature(device, security_v2.request_payload(
                        fieldToValue['challenge'], fieldToValue['nonce'], self.token.token, security_v2.sha256_hex(public), ''))
                    requests.append(fieldToValue)
                barrier = threading.Barrier(4)
                def issue(fieldToValue):
                    close_old_connections()
                    try:
                        barrier.wait(timeout=10)
                        security_v2.lease_response(fieldToValue)
                        return 'ok'
                    except security_v2.SecurityError as error:
                        return error.code
                    finally:
                        close_old_connections()
                with ThreadPoolExecutor(max_workers=4) as workers:
                    results = list(workers.map(issue, requests))
        self.assertEqual(results.count('ok'), 1, results)
        self.assertEqual(results.count('session_limit'), 3, results)
        self.assertEqual(GhostTokenDevice.objects.count(), 1)
        self.assertEqual(GhostSecuritySession.objects.count(), 1)

    def test_simultaneous_ai_requests_charge_provider_once(self):
        sessions = []
        for _ in range(2):
            security = GhostSecuritySession.objects.create(session=GhostSession.objects.create(access_token=self.token),
                access_token=self.token, device_key_hash=secrets.token_hex(32), producer_secret=secrets.token_hex(32),
                viewer_secret=secrets.token_hex(32), lease_expires_at=timezone.now()+timedelta(minutes=10))
            sessions.append(security)
        entered, release = threading.Event(), threading.Event()
        def complete(_messages):
            entered.set()
            if not release.wait(timeout=10):
                raise RuntimeError('Timed out waiting for competing request')
            return '42'
        def ask(security):
            close_old_connections()
            try:
                return ai_service.chat(security.session.session_id, security.producer_secret, str(uuid.uuid4()), b'\xff\xd8\xfffixture\xff\xd9')
            except security_v2.SecurityError as error:
                return error.code
            finally:
                close_old_connections()
        with patch('ghost_note.ai_provider.complete', side_effect=complete) as provider:
            with ThreadPoolExecutor(max_workers=2) as workers:
                first = workers.submit(ask, sessions[0])
                self.assertTrue(entered.wait(timeout=10))
                try:
                    second = workers.submit(ask, sessions[1]).result(timeout=10)
                    self.assertEqual(second, 'ai_cooldown')
                finally:
                    release.set()
                self.assertTrue(first.result(timeout=10)['ok'])
            self.assertEqual(provider.call_count, 1)
        self.assertEqual(GhostAIRequest.objects.count(), 1)
