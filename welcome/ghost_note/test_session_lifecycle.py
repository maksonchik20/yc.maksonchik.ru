from datetime import timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.utils import timezone

from .models import GhostAccessToken, GhostSecuritySession
from .security_v2 import SESSION_IDLE_SECONDS, SecurityError, require_security
from .test_security_v2 import SecurityFixture


class SessionLifecycleTests(SecurityFixture):
    def setUp(self):
        super().setUp()
        GhostAccessToken.objects.filter(pk=self.token.pk).update(max_devices=2, max_concurrent_sessions=1)

    def other_device(self):
        self.device = ec.generate_private_key(ec.SECP256R1())
        self.public_bytes = self.device.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

    def close(self, security, credential=None):
        return self.client.post('/ghost/v2/session/close/', data=b'', content_type='application/json',
            HTTP_X_SESSION_ID=str(security.session.session_id),
            HTTP_X_GHOST_LEASE=credential or security.producer_secret)

    def stale(self, security):
        GhostSecuritySession.objects.filter(pk=security.pk).update(
            last_seen_at=timezone.now()-timedelta(seconds=SESSION_IDLE_SECONDS+1))

    def test_close_releases_slot_immediately_for_other_device(self):
        first, _ = self.authorize()
        self.assertEqual(self.close(first).status_code, 200)
        self.other_device()
        second, _ = self.authorize()
        self.assertNotEqual(first.pk, second.pk)

    def test_close_is_idempotent_and_cannot_revoke_replacement(self):
        first, _ = self.authorize()
        self.assertEqual(self.close(first).status_code, 200)
        first.refresh_from_db()
        closed_at = first.revoked_at
        second, _ = self.authorize()
        self.assertEqual(self.close(first).status_code, 200)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.revoked_at, closed_at)
        self.assertIsNone(second.revoked_at)

    def test_expired_or_disabled_token_can_still_close(self):
        first, _ = self.authorize()
        GhostAccessToken.objects.filter(pk=self.token.pk).update(expires_at=timezone.now()-timedelta(days=1))
        GhostSecuritySession.objects.filter(pk=first.pk).update(lease_expires_at=timezone.now()-timedelta(seconds=1))
        self.assertEqual(self.close(first).status_code, 200)
        first.refresh_from_db()
        self.assertIsNotNone(first.revoked_at)

    def test_viewer_wrong_or_missing_credentials_cannot_close(self):
        first, _ = self.authorize()
        for credential in [first.viewer_secret, '0'*64, 'bad']:
            self.assertEqual(self.close(first, credential).status_code, 401)
        self.assertEqual(self.client.post('/ghost/v2/session/close/').status_code, 400)
        first.refresh_from_db()
        self.assertIsNone(first.revoked_at)

    def test_expired_lease_cannot_revive_while_other_device_occupies_slot(self):
        first, _ = self.authorize()
        original = self.device, self.public_bytes
        GhostSecuritySession.objects.filter(pk=first.pk).update(lease_expires_at=timezone.now()-timedelta(seconds=1))
        self.other_device()
        self.authorize()
        self.device, self.public_bytes = original
        response = self.post('/ghost/v2/lease/', self.proof(first.session.session_id))
        self.assertNotEqual(response.status_code, 200)
        self.assertEqual(GhostSecuritySession.objects.filter(revoked_at__isnull=True).count(), 1)

    def test_renewal_also_checks_capacity_for_preexisting_overlapping_sessions(self):
        GhostAccessToken.objects.filter(pk=self.token.pk).update(max_concurrent_sessions=0)
        first, _ = self.authorize()
        original = self.device, self.public_bytes
        self.other_device()
        self.authorize()
        GhostAccessToken.objects.filter(pk=self.token.pk).update(max_concurrent_sessions=1)
        self.device, self.public_bytes = original
        original_expiry = first.lease_expires_at
        response = self.post('/ghost/v2/lease/', self.proof(first.session.session_id))
        self.assertEqual(response.json()['error'], 'session_limit')
        first.refresh_from_db()
        self.assertEqual(first.lease_expires_at, original_expiry)

    def test_crash_frees_slot_after_idle_timeout_and_old_session_stays_revoked(self):
        first, _ = self.authorize()
        original = self.device, self.public_bytes
        self.stale(first)
        self.other_device()
        self.authorize()
        first.refresh_from_db()
        self.assertIsNotNone(first.revoked_at)
        self.device, self.public_bytes = original
        self.assertNotEqual(self.post('/ghost/v2/lease/', self.proof(first.session.session_id)).status_code, 200)
        with self.assertRaises(SecurityError):
            require_security(str(first.session.session_id), first.producer_secret)

    def test_live_producer_keeps_slot_and_viewer_does_not(self):
        first, _ = self.authorize()
        self.stale(first)
        require_security(str(first.session.session_id), first.producer_secret)
        self.other_device()
        response = self.post('/ghost/v2/lease/', self.proof())
        self.assertEqual(response.json()['error'], 'session_limit')
        self.stale(first)
        require_security(str(first.session.session_id), first.viewer_secret, viewer=True)
        self.authorize()
        first.refresh_from_db()
        self.assertIsNotNone(first.revoked_at)

    def test_legacy_null_heartbeat_falls_back_to_creation_time(self):
        first, _ = self.authorize()
        GhostSecuritySession.objects.filter(pk=first.pk).update(last_seen_at=None)
        self.other_device()
        self.assertEqual(self.post('/ghost/v2/lease/', self.proof()).json()['error'], 'session_limit')
        GhostSecuritySession.objects.filter(pk=first.pk).update(
            created_at=timezone.now()-timedelta(seconds=SESSION_IDLE_SECONDS+1))
        self.authorize()

    def test_temporary_outage_can_renew_if_no_other_device_claimed_slot(self):
        first, _ = self.authorize()
        self.stale(first)
        GhostSecuritySession.objects.filter(pk=first.pk).update(lease_expires_at=timezone.now()-timedelta(seconds=1))
        self.authorize(first.session.session_id)
        first.refresh_from_db()
        self.assertIsNone(first.revoked_at)
        self.assertGreater(first.lease_expires_at, timezone.now())

    def test_closed_session_cannot_renew(self):
        first, _ = self.authorize()
        self.close(first)
        self.assertNotEqual(self.post('/ghost/v2/lease/', self.proof(first.session.session_id)).status_code, 200)
