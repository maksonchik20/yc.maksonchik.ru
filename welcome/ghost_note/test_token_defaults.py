import uuid
from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from .admin import GhostAccessTokenAdminForm
from .models import GhostAccessToken, GhostPurchaseOrder, GhostTelegramContact, GhostUser
from .panel_api import create_token
from .purchase import fulfill_purchase_order
from .trial import issue_trial_token


class TokenDefaultTests(TestCase):
    def setUp(self):
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('External HTTP forbidden'))
        network.start()
        self.addCleanup(network.stop)

    def assert_limits(self, token):
        token.refresh_from_db()
        self.assertEqual((token.max_devices, token.max_concurrent_sessions), (2, 1))

    def test_admin_initial_and_missing_fields_use_defaults(self):
        initial = GhostAccessTokenAdminForm()
        self.assertEqual(initial['max_devices'].value(), 2)
        self.assertEqual(initial['max_concurrent_sessions'].value(), 1)
        form = GhostAccessTokenAdminForm(data={
            'token_type': 'test', 'allow_remote': 'on', 'is_active': 'on',
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assert_limits(form.save())

    def test_owner_panel_uses_defaults(self):
        user = GhostUser.objects.create(name='Defaults test')
        result, status = create_token({
            'request_id': str(uuid.uuid4()), 'user_id': user.pk,
            'token_type': 'test', 'allow_local': True, 'allow_remote': True,
        })
        self.assertEqual(status, 201, result)
        self.assert_limits(GhostAccessToken.objects.get(pk=result['id']))

    def test_trial_uses_defaults(self):
        GhostTelegramContact.objects.create(telegram_user_id=991122)
        with patch('ghost_note.telegram_notify.notify_trial_token'):
            self.assert_limits(issue_trial_token(telegram_user_id=991122))

    def test_purchase_uses_defaults(self):
        order = GhostPurchaseOrder.objects.create(
            customer_name='Defaults purchase', starts_at=timezone.now(),
            duration_minutes=60, amount=1000, access_type='remote',
        )
        self.assert_limits(fulfill_purchase_order(order))

    def test_explicit_limits_survive_saving_existing_token(self):
        token = GhostAccessToken.objects.create(token_type='test',
            expires_at=timezone.now()+timedelta(hours=1), max_devices=5, max_concurrent_sessions=3)
        token.label = 'Updated note'
        token.save()
        token.refresh_from_db()
        self.assertEqual((token.max_devices, token.max_concurrent_sessions), (5, 3))


class TokenDefaultsMigrationTests(TransactionTestCase):
    def test_migration_preserves_existing_limits_and_changes_new_defaults(self):
        previous = [('ghost_note', '0021_activity_security')]
        target = [('ghost_note', '0022_token_device_defaults')]
        executor = MigrationExecutor(connection)
        executor.migrate(previous)
        historical_token = executor.loader.project_state(previous).apps.get_model('ghost_note', 'GhostAccessToken')
        for index, limits in enumerate([(0, 0), (1, 1), (5, 3)]):
            historical_token.objects.create(token='BEFORE-{}'.format(index), token_type='test',
                expires_at=timezone.now()+timedelta(hours=1),
                max_devices=limits[0], max_concurrent_sessions=limits[1])
        try:
            MigrationExecutor(connection).migrate(target)
            for index, limits in enumerate([(0, 0), (1, 1), (5, 3)]):
                token = GhostAccessToken.objects.get(token='BEFORE-{}'.format(index))
                self.assertEqual((token.max_devices, token.max_concurrent_sessions), limits)
            token = GhostAccessToken.objects.create(token_type='test', expires_at=timezone.now()+timedelta(hours=1))
            self.assertEqual((token.max_devices, token.max_concurrent_sessions), (2, 1))
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
