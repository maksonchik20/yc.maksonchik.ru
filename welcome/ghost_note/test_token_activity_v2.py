from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test.utils import CaptureQueriesContext

from .auth import TOKEN_ACTIVITY_WRITE_SECONDS
from .models import GhostAccessToken
from .test_security_v2 import SecurityFixture


class TokenActivityV2Tests(SecurityFixture):
    def test_issue_and_renewal_skip_token_save_and_throttle_activity_only(self):
        with patch.object(GhostAccessToken, 'save', side_effect=AssertionError('token save called')):
            first, _lines = self.authorize()
            self.token.refresh_from_db()
            first_used = self.token.last_used_at
            self.assertIsNotNone(first_used)
            with CaptureQueriesContext(connection) as queries:
                renewed, _lines = self.authorize(first.session.session_id)
        updates = [query['sql'] for query in queries if query['sql'].lstrip().upper().startswith('UPDATE')]
        self.assertFalse(any('ghost_note_ghostaccesstoken' in sql for sql in updates))
        self.assertTrue(any('ghost_note_ghostsecuritysession' in sql for sql in updates))
        self.assertEqual(renewed.pk, first.pk)
        self.token.refresh_from_db()
        self.assertEqual(self.token.last_used_at, first_used)

    def test_renewal_updates_activity_after_interval_without_referral_sync(self):
        first, _lines = self.authorize()
        self.token.refresh_from_db()
        previous = self.token.last_used_at - timedelta(seconds=TOKEN_ACTIVITY_WRITE_SECONDS + 1)
        GhostAccessToken.objects.filter(pk=self.token.pk).update(last_used_at=previous)
        with patch.object(GhostAccessToken, 'sync_referral_commission', side_effect=AssertionError('referral sync called')):
            self.authorize(first.session.session_id)
        self.token.refresh_from_db()
        self.assertGreater(self.token.last_used_at, previous)
