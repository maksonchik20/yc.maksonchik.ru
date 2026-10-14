"""Run on PostgreSQL as well as the lightweight SQLite regression database."""

import base64
import uuid
from types import SimpleNamespace

from django.db import IntegrityError, connection, transaction
from django.test import SimpleTestCase

from .auth import session_id_for_access_token
from .fields import (TEXT_ENCODING_PREFIX, NullSafeTextField,
                     decode_postgresql_text, encode_postgresql_text)
from .models import GhostAccessToken, GhostSession, GhostTextMessage, GhostUser
from .test_security_v2 import SecurityFixture


class PostgreSQLTextEncodingTests(SimpleTestCase):
    def test_encoding_round_trip_and_prefix_collision(self):
        values = ['', 'обычный текст 😀', 'left\x00right\b\f\t\r\n',
                  TEXT_ENCODING_PREFIX, TEXT_ENCODING_PREFIX + 'bGl0ZXJhbA==',
                  TEXT_ENCODING_PREFIX + '\x00😀']
        for value in values:
            with self.subTest(value=value):
                encoded = encode_postgresql_text(value)
                self.assertNotIn('\x00', encoded)
                self.assertEqual(decode_postgresql_text(encoded), value)

    def test_ordinary_strings_and_null_remain_unchanged(self):
        for value in ('regular text', None):
            self.assertEqual(encode_postgresql_text(value), value)
            self.assertEqual(decode_postgresql_text(value), value)

    def test_mysql_sqlite_storage_is_not_changed(self):
        value = TEXT_ENCODING_PREFIX + 'text\x00'
        field = NullSafeTextField()
        for vendor in ('mysql', 'sqlite'):
            backend = SimpleNamespace(vendor=vendor)
            self.assertEqual(field.get_db_prep_value(value, backend), value)
            self.assertEqual(field.from_db_value(value, None, backend), value)


class DatabaseCompatibilityTests(SecurityFixture):
    def test_legacy_mixed_case_token_remains_valid(self):
        response = self.post('/ghost/auth/', {'token': self.token.token.lower()})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['ok'])
        session_response = self.post('/ghost/session/', {'token': self.token.token.lower()})
        self.assertEqual(session_response.status_code, 200, session_response.content)
        self.assertEqual(GhostSession.objects.get().access_token_id, self.token.pk)

    def test_signed_lease_preserves_supplied_token_case(self):
        original = self.token.token
        self.token.token = original.lower()
        security, lines = self.authorize()
        self.assertEqual(security.access_token_id, self.token.pk)
        self.assertEqual(base64.b64decode(self.post('/ghost/v2/lease/', self.proof(
            security.session.session_id)).json()['payload']).decode().split('\n')[4], lines[4])
        self.token.refresh_from_db()
        self.assertEqual(self.token.token, original)

    def test_legacy_uuid_case_reuses_existing_session(self):
        session = GhostSession.objects.create(access_token=self.token)
        response = self.client.post('/ghost/screenshot/', b'legacy-jpeg',
            content_type='image/jpeg', HTTP_X_SESSION_ID=session.session_id.upper(),
            HTTP_X_ACCESS_TOKEN=self.token.token.lower())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(GhostSession.objects.count(), 1)
        response = self.client.get('/ghost/screenshot/get/', {'session_id': session.session_id.upper()})
        self.assertEqual(response.content, b'legacy-jpeg')

    def test_existing_uppercase_legacy_uuid_remains_readable(self):
        session = GhostSession.objects.create(session_id=str(uuid.uuid4()).upper(),
                                              access_token=self.token, screenshot=b'old-jpeg')
        response = self.client.get('/ghost/screenshot/get/', {'session_id': session.session_id.lower()})
        self.assertEqual(response.content, b'old-jpeg')

    def test_register_and_viewer_reuse_uppercase_deterministic_legacy_uuid(self):
        session = GhostSession.objects.create(
            session_id=session_id_for_access_token(self.token.token).upper(), access_token=self.token)
        response = self.post('/ghost/session/', {'token': self.token.token.lower()})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['session_id'], session.session_id)
        viewer = self.client.get('/ghost/viewer/', {'token': self.token.token.lower()})
        self.assertEqual(viewer.status_code, 200, viewer.content)
        self.assertEqual(GhostSession.objects.count(), 1)
        self.assertEqual(GhostSession.objects.get().pk, session.pk)

    def test_text_storage_round_trip_preserves_nul_and_reserved_prefix(self):
        session = GhostSession.objects.create(access_token=self.token)
        values = ['left\x00right 😀', TEXT_ENCODING_PREFIX + 'bGl0ZXJhbA==', 'next message']
        for value in values:
            response = self.post('/ghost/text/', {'session_id': session.session_id,
                'text': value, 'idempotency_key': str(uuid.uuid4())})
            self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(list(GhostTextMessage.objects.order_by('pk').values_list('text', flat=True)), values)
        response = self.client.get('/ghost/text/poll/', {'session_id': session.session_id},
                                   HTTP_X_ACCESS_TOKEN=self.token.token)
        self.assertEqual(response.json()['messages'], values)
        self.assertFalse(GhostTextMessage.objects.filter(delivered=False).exists())

    def test_postgresql_keeps_case_insensitive_identity_uniqueness(self):
        if connection.vendor != 'postgresql':
            self.skipTest('PostgreSQL expression indexes replace MySQL case-insensitive unique keys')
        session = GhostSession.objects.create(access_token=self.token)
        user = GhostUser.objects.create(name='case-test', referral_key='UPPERKEY')
        for model, fieldToValue in (
            (GhostAccessToken, {'token': self.token.token.lower(), 'expires_at': self.token.expires_at}),
            (GhostSession, {'session_id': session.session_id.upper()}),
            (GhostUser, {'name': 'duplicate-key', 'referral_key': user.referral_key.lower()}),
        ):
            with self.subTest(model=model.__name__), self.assertRaises(IntegrityError), transaction.atomic():
                model.objects.bulk_create([model(**fieldToValue)])

    def test_activity_lock_works_with_nullable_token_relation(self):
        security, _lines = self.authorize()
        response = self.post('/ghost/v2/activity/consent/', {'enabled': True, 'mode': 'remote'},
            HTTP_X_SESSION_ID=security.session.session_id, HTTP_X_GHOST_LEASE=security.producer_secret)
        self.assertEqual(response.status_code, 200, response.content)
        security.refresh_from_db()
        self.assertIsNotNone(security.recording_consent_at)
