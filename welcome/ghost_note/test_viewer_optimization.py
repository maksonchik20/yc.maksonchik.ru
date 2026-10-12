from datetime import timedelta
import uuid
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .models import GhostAccessToken, GhostAIRequest, GhostAIThread, GhostSecuritySession, GhostSession, GhostTokenDevice
from .security_v2 import require_security
from .views_v2 import _screen_etag


class ViewerOptimizationTests(TestCase):
    def setUp(self):
        self.token = GhostAccessToken.objects.create(token='VTEST1', token_type='real', payment_amount=1,
            starts_at=timezone.now()-timedelta(minutes=5), expires_at=timezone.now()+timedelta(hours=1))
        self.session = GhostSession.objects.create(access_token=self.token)
        self.security = GhostSecuritySession.objects.create(session=self.session, access_token=self.token,
            device_key_hash='a'*64, producer_secret='b'*64, viewer_secret='c'*64,
            lease_expires_at=timezone.now()+timedelta(minutes=10))
        self.image = b'\xff\xd8test-jpeg\xff\xd9'

    def screenshot(self, etag=None, **headerToOverride):
        headerToValue = {'HTTP_X_GHOST_VIEWER': self.security.viewer_secret}
        if etag is not None:
            headerToValue['HTTP_IF_NONE_MATCH'] = etag
        headerToValue.update(headerToOverride)
        return self.client.get('/ghost/v2/screenshot/get/', {'session_id': self.session.session_id}, **headerToValue)

    def save_image(self, image=None):
        self.session.save_screenshot(self.image if image is None else image)
        return self.screenshot()

    def assert_no_blob_selected(self, queries):
        for item in queries:
            sql = item['sql'].replace('`', '"')
            for table, column in [('ghost_note_ghostsession', 'screenshot'), ('ghost_note_ghostairequest', 'image')]:
                field = '"{}"."{}"'.format(table, column)
                self.assertNotIn(field, sql.replace('LENGTH({})'.format(field), ''), sql)

    def ai_image(self, image=None, device=None):
        GhostAccessToken.objects.filter(pk=self.token.pk).update(allow_ai=True)
        thread, _created = GhostAIThread.objects.get_or_create(access_token=self.token,
            device_key_hash=device or self.security.device_key_hash)
        return GhostAIRequest.objects.create(thread=thread, request_id=uuid.uuid4(),
            conversation_id=thread.conversation_id, image_hash='d'*64,
            image=self.image if image is None else image)

    def test_fresh_image_and_unchanged_poll_preserve_headers_without_blob_query(self):
        first = self.save_image()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.content, self.image)
        self.assertEqual(first['X-Ghost-Screen-Source'], 'live')
        with CaptureQueriesContext(connection) as queries:
            response = self.screenshot(first['ETag'])
        self.assertEqual(response.status_code, 304)
        self.assertEqual(response.content, b'')
        self.assertEqual(response['ETag'], first['ETag'])
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(response['Referrer-Policy'], 'no-referrer')
        self.assert_no_blob_selected(queries.captured_queries)

    def test_reload_without_conditional_header_returns_image(self):
        self.save_image()
        response = self.screenshot()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.image)

    def test_changed_image_gets_new_version(self):
        first = self.save_image()
        self.session.save_screenshot(b'new image')
        response = self.screenshot(first['ETag'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'new image')
        self.assertNotEqual(response['ETag'], first['ETag'])

    def test_empty_or_cleared_image_never_returns_stale_304(self):
        for empty in (None, b''):
            with self.subTest(empty=empty):
                first = self.save_image()
                # Old/admin clients may clear a BLOB without clearing its timestamp.
                GhostSession.objects.filter(pk=self.session.pk).update(screenshot=empty)
                with CaptureQueriesContext(connection) as queries:
                    response = self.screenshot(first['ETag'])
                self.assertEqual(response.status_code, 204)
                self.assertFalse(response.has_header('ETag'))
                self.assert_no_blob_selected(queries.captured_queries)

    def test_legacy_image_without_timestamp_is_not_conditionally_cached(self):
        GhostSession.objects.filter(pk=self.session.pk).update(screenshot=self.image, screenshot_updated_at=None)
        response = self.screenshot('*')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.image)
        self.assertFalse(response.has_header('ETag'))

    def test_conditional_variants_are_supported_only_after_authorization(self):
        first = self.save_image()
        for tag in ('*', 'W/' + first['ETag'], '"other", ' + first['ETag']):
            with self.subTest(tag=tag):
                self.assertEqual(self.screenshot(tag).status_code, 304)
                self.assertEqual(self.screenshot(tag, HTTP_X_GHOST_VIEWER=self.security.producer_secret).status_code, 401)

    def test_revoked_or_expired_session_rejects_matching_etag(self):
        etag = self.save_image()['ETag']
        for fieldToValue in ({'revoked_at': timezone.now()}, {'lease_expires_at': timezone.now()-timedelta(seconds=1)}):
            with self.subTest(fieldToValue=fieldToValue):
                GhostSecuritySession.objects.filter(pk=self.security.pk).update(**fieldToValue)
                self.assertEqual(self.screenshot(etag).status_code, 401)
                GhostSecuritySession.objects.filter(pk=self.security.pk).update(revoked_at=None,
                    lease_expires_at=timezone.now()+timedelta(minutes=10))

    def test_invalid_token_or_changed_permissions_reject_matching_etag(self):
        etag = self.save_image()['ETag']
        for fieldToValue, status in (({'is_active': False}, 401),
                ({'expires_at': timezone.now()-timedelta(seconds=1)}, 401),
                ({'allow_remote': False, 'allow_ai': False}, 403)):
            with self.subTest(fieldToValue=fieldToValue):
                GhostAccessToken.objects.filter(pk=self.token.pk).update(**fieldToValue)
                self.assertEqual(self.screenshot(etag).status_code, status)
                GhostAccessToken.objects.filter(pk=self.token.pk).update(is_active=True,
                    expires_at=timezone.now()+timedelta(hours=1), allow_remote=True, allow_ai=False)

    def test_blocked_device_rejects_matching_etag(self):
        etag = self.save_image()['ETag']
        GhostTokenDevice.objects.create(access_token=self.token, device_key_hash=self.security.device_key_hash, blocked=True)
        self.assertEqual(self.screenshot(etag).status_code, 403)

    def test_auth_defers_blob_but_accessing_image_remains_compatible(self):
        self.save_image()
        with CaptureQueriesContext(connection) as queries:
            security = require_security(self.session.session_id, self.security.viewer_secret, viewer=True)
        self.assertIn('screenshot', security.session.get_deferred_fields())
        self.assert_no_blob_selected(queries.captured_queries)
        with self.assertNumQueries(1):
            self.assertEqual(bytes(security.session.screenshot), self.image)

    def test_text_poll_no_longer_selects_screenshot_blob(self):
        self.save_image()
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get('/ghost/v2/text/poll/', {'session_id': self.session.session_id},
                HTTP_X_GHOST_LEASE=self.security.producer_secret)
        self.assertEqual(response.status_code, 200)
        self.assert_no_blob_selected(queries.captured_queries)

    def test_ai_fallback_unchanged_poll_does_not_load_blob(self):
        self.ai_image()
        first = self.screenshot()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first['X-Ghost-Screen-Source'], 'ai-request')
        with CaptureQueriesContext(connection) as queries:
            response = self.screenshot(first['ETag'])
        self.assertEqual(response.status_code, 304)
        self.assert_no_blob_selected(queries.captured_queries)

    def test_new_ai_request_changes_version_even_when_timestamp_and_size_match(self):
        first_item = self.ai_image()
        first = self.screenshot()
        second_item = self.ai_image()
        GhostAIRequest.objects.filter(pk=second_item.pk).update(created_at=first_item.created_at)
        response = self.screenshot(first['ETag'])
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response['ETag'], first['ETag'])

    def test_ai_fallback_does_not_cross_device_or_session(self):
        item = self.ai_image(device='f'*64)
        self.assertEqual(self.screenshot('*').status_code, 204)
        item = self.ai_image()
        GhostAIRequest.objects.filter(pk=item.pk).update(created_at=self.security.created_at-timedelta(seconds=1))
        self.assertEqual(self.screenshot('*').status_code, 204)

    def test_cleared_live_image_can_fall_back_to_current_ai_image(self):
        self.ai_image(b'AI image')
        live = self.save_image()
        GhostSession.objects.filter(pk=self.session.pk).update(screenshot=None)
        response = self.screenshot(live['ETag'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'AI image')
        self.assertEqual(response['X-Ghost-Screen-Source'], 'ai-request')

    def test_latest_empty_ai_image_does_not_resurrect_an_older_image(self):
        self.ai_image()
        first = self.screenshot()
        self.ai_image(b'')
        self.assertEqual(self.screenshot(first['ETag']).status_code, 204)

    def test_image_changed_during_read_is_not_labelled_with_old_etag(self):
        self.save_image()
        original_time = self.session.screenshot_updated_at
        old_etag = _screen_etag('live', self.session.pk, original_time, len(self.image))

        def update_after_metadata(source, image_id, captured_at, image_size):
            if captured_at == original_time:
                self.session.save_screenshot(b'newer image')
            return _screen_etag(source, image_id, captured_at, image_size)

        with patch('ghost_note.views_v2._screen_etag', side_effect=update_after_metadata):
            response = self.screenshot()
        self.assertEqual(response.content, b'newer image')
        self.assertNotEqual(response['ETag'], old_etag)
        self.assertEqual(self.screenshot(response['ETag']).status_code, 304)

    def test_image_cleared_during_read_returns_empty_response(self):
        self.save_image()

        def clear_after_metadata(*args):
            GhostSession.objects.filter(pk=self.session.pk).update(screenshot=None)
            return _screen_etag(*args)

        with patch('ghost_note.views_v2._screen_etag', side_effect=clear_after_metadata):
            self.assertEqual(self.screenshot().status_code, 204)
