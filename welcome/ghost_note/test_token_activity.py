from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .auth import TOKEN_ACTIVITY_WRITE_SECONDS, record_token_activity, validate_access_token
from .models import GhostAccessToken, GhostReferralCommission, GhostUser


class TokenActivityTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.token = GhostAccessToken.objects.create(
            token='ACTIVITY', token_type='real', payment_amount=100,
            starts_at=self.now - timedelta(hours=1),
            expires_at=self.now + timedelta(hours=1),
        )

    def validate_at(self, now, value=None):
        with patch('ghost_note.auth.timezone.now', return_value=now):
            return validate_access_token(self.token.token if value is None else value)

    def test_first_use_updates_activity_without_save_side_effects(self):
        with patch.object(GhostAccessToken, 'save', side_effect=AssertionError('model save called')):
            with self.assertNumQueries(2):
                token, status, expiry = self.validate_at(self.now)
        self.assertEqual(status, 'ok')
        self.assertEqual(token.last_used_at, self.now)
        self.assertEqual(expiry, self.token.expires_at)
        self.token.refresh_from_db()
        self.assertEqual(self.token.last_used_at, self.now)

    def test_recent_activity_only_reads_token_and_writes_at_exact_boundary(self):
        self.validate_at(self.now)
        just_before = self.now + timedelta(seconds=TOKEN_ACTIVITY_WRITE_SECONDS, microseconds=-1)
        with self.assertNumQueries(1):
            token, status, _expiry = self.validate_at(just_before)
        self.assertEqual(status, 'ok')
        self.assertEqual(token.last_used_at, self.now)
        boundary = self.now + timedelta(seconds=TOKEN_ACTIVITY_WRITE_SECONDS)
        with self.assertNumQueries(2):
            token, status, _expiry = self.validate_at(boundary)
        self.assertEqual(token.last_used_at, boundary)
        self.assertEqual(status, 'ok')

    def test_one_minute_of_polling_has_two_activity_updates_and_fresh_reads(self):
        with CaptureQueriesContext(connection) as queries:
            for second in range(60):
                self.assertEqual(self.validate_at(self.now + timedelta(seconds=second))[1], 'ok')
        self.assertEqual(len(queries), 62)
        updates = [query['sql'] for query in queries if query['sql'].lstrip().upper().startswith('UPDATE')]
        self.assertEqual(len(updates), 2)
        self.assertTrue(all('ghost_note_ghostaccesstoken' in sql for sql in updates))

    def test_empty_and_unknown_tokens_do_not_write(self):
        with self.assertNumQueries(0):
            self.assertEqual(self.validate_at(self.now, ' '), (None, 'invalid', None))
        with self.assertNumQueries(1):
            self.assertEqual(self.validate_at(self.now, 'UNKNOWN'), (None, 'invalid', None))
        self.token.refresh_from_db()
        self.assertIsNone(self.token.last_used_at)

    def test_token_normalization_is_preserved(self):
        token, status, _expiry = self.validate_at(self.now, '  ACTIVITY  ')
        self.assertEqual(status, 'ok')
        self.assertEqual(token.pk, self.token.pk)

    def test_revocation_is_observed_inside_throttle_window(self):
        self.validate_at(self.now)
        GhostAccessToken.objects.filter(pk=self.token.pk).update(is_active=False)
        with self.assertNumQueries(1):
            self.assertEqual(self.validate_at(self.now + timedelta(seconds=1)), (None, 'invalid', None))
        self.token.refresh_from_db()
        self.assertEqual(self.token.last_used_at, self.now)

    def test_changed_schedule_is_observed_inside_throttle_window(self):
        self.validate_at(self.now)
        starts_at = self.now + timedelta(minutes=5)
        GhostAccessToken.objects.filter(pk=self.token.pk).update(starts_at=starts_at)
        with self.assertNumQueries(1):
            self.assertEqual(self.validate_at(self.now), (None, 'not_started', starts_at))
        GhostAccessToken.objects.filter(pk=self.token.pk).update(starts_at=self.token.starts_at, expires_at=self.now)
        with self.assertNumQueries(1):
            self.assertEqual(self.validate_at(self.now), (None, 'expired', self.now))

    def test_expiry_boundary_is_checked_even_with_recent_activity(self):
        expires_at = self.now + timedelta(seconds=2)
        GhostAccessToken.objects.filter(pk=self.token.pk).update(expires_at=expires_at)
        self.validate_at(self.now)
        with self.assertNumQueries(1):
            self.assertEqual(self.validate_at(expires_at), (None, 'expired', expires_at))
        self.token.refresh_from_db()
        self.assertEqual(self.token.last_used_at, self.now)

    def test_current_permissions_and_expiry_are_not_cached(self):
        self.validate_at(self.now)
        expires_at = self.now + timedelta(days=1)
        GhostAccessToken.objects.filter(pk=self.token.pk).update(
            allow_remote=False, allow_ai=True, expires_at=expires_at,
        )
        with self.assertNumQueries(1):
            token, status, expiry = self.validate_at(self.now + timedelta(seconds=1))
        self.assertEqual(status, 'ok')
        self.assertFalse(token.allow_remote)
        self.assertTrue(token.allow_ai)
        self.assertEqual(expiry, expires_at)

    def test_stale_concurrent_readers_only_update_once(self):
        first = GhostAccessToken.objects.get(pk=self.token.pk)
        second = GhostAccessToken.objects.get(pk=self.token.pk)
        with self.assertNumQueries(1):
            self.assertTrue(record_token_activity(first, self.now))
        with self.assertNumQueries(1):
            self.assertFalse(record_token_activity(second, self.now + timedelta(seconds=1)))
        self.token.refresh_from_db()
        self.assertEqual(self.token.last_used_at, self.now)
        # The stale object remains safe at the next eligible interval as well.
        boundary = self.now + timedelta(seconds=TOKEN_ACTIVITY_WRITE_SECONDS)
        self.assertTrue(record_token_activity(second, boundary))
        self.assertFalse(record_token_activity(first, boundary))
        self.token.refresh_from_db()
        self.assertEqual(self.token.last_used_at, boundary)

    def test_stale_reader_cannot_overwrite_newer_timestamp(self):
        older = GhostAccessToken.objects.get(pk=self.token.pk)
        newer = GhostAccessToken.objects.get(pk=self.token.pk)
        self.assertTrue(record_token_activity(newer, self.now + timedelta(seconds=2)))
        self.assertFalse(record_token_activity(older, self.now))
        self.token.refresh_from_db()
        self.assertEqual(self.token.last_used_at, self.now + timedelta(seconds=2))

    def test_future_timestamp_does_not_disable_auth_or_move_backwards(self):
        future = self.now + timedelta(seconds=30)
        GhostAccessToken.objects.filter(pk=self.token.pk).update(last_used_at=future)
        with self.assertNumQueries(1):
            token, status, _expiry = self.validate_at(self.now)
        self.assertEqual(status, 'ok')
        self.assertEqual(token.last_used_at, future)

    def test_activity_changes_no_license_or_referral_fields(self):
        referrer = GhostUser.objects.create(name='Referrer')
        user = GhostUser.objects.create(name='Referred', referred_by=referrer)
        self.token.user = user
        self.token.save(update_fields=['user'])
        commission = GhostReferralCommission.objects.get(token=self.token)
        commission.is_paid = True
        commission.save(update_fields=['is_paid'])
        fieldToCommissionValue = GhostReferralCommission.objects.values().get(pk=commission.pk)
        fieldToTokenValue = GhostAccessToken.objects.values().get(pk=self.token.pk)
        with patch.object(GhostAccessToken, 'sync_referral_commission', side_effect=AssertionError('referral sync called')):
            self.validate_at(self.now)
            self.validate_at(self.now + timedelta(seconds=TOKEN_ACTIVITY_WRITE_SECONDS))
        self.assertEqual(GhostReferralCommission.objects.values().get(pk=commission.pk), fieldToCommissionValue)
        fieldToActualValue = GhostAccessToken.objects.values().get(pk=self.token.pk)
        fieldToTokenValue.pop('last_used_at')
        fieldToActualValue.pop('last_used_at')
        self.assertEqual(fieldToActualValue, fieldToTokenValue)

    def test_financial_save_still_creates_and_updates_commission(self):
        referrer = GhostUser.objects.create(name='Referrer')
        user = GhostUser.objects.create(name='Referred', referred_by=referrer)
        self.token.user = user
        self.token.save(update_fields=['user'])
        commission = GhostReferralCommission.objects.get(token=self.token)
        self.assertEqual(commission.commission_amount, Decimal('20.00'))
        commission.is_paid = True
        commission.save(update_fields=['is_paid'])
        self.token.payment_amount = Decimal('200.00')
        self.token.save(update_fields=['payment_amount'])
        commission.refresh_from_db()
        self.assertEqual(commission.payment_amount, Decimal('200.00'))
        self.assertEqual(commission.commission_amount, Decimal('40.00'))
        self.assertTrue(commission.is_paid)
        self.token.token_type = GhostAccessToken.TokenType.TEST
        self.token.save(update_fields=['token_type', 'payment_amount'])
        self.assertFalse(GhostReferralCommission.objects.filter(token=self.token).exists())
