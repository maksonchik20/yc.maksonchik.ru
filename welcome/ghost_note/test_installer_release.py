import importlib
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from . import purchase_tips
from .models import GhostPurchaseOrder, GhostTelegramOutbox
from .telegram_bot import process_telegram_update
from .telegram_notify import notify_purchase_token


class InstallerReleaseTests(TransactionTestCase):
    def setUp(self):
        configuration = override_settings(GHOST_NOTE_BOT_TOKEN='test-secret',
            GHOST_NOTE_ADMIN_CHAT_ID='',
            GHOST_NOTE_DOWNLOAD_URL='https://disk.yandex.ru/d/fixture-windows',
            GHOST_NOTE_INSTALLER_FILE_ID='new-installer-fixture')
        configuration.enable()
        importlib.reload(purchase_tips)
        def restore_settings():
            configuration.disable()
            importlib.reload(purchase_tips)
        self.addCleanup(restore_settings)
        api_patch = patch('ghost_note.telegram_notify._call_telegram_api',
            return_value={'ok': True, 'result': {'message_id': 1}})
        self.api = api_patch.start()
        self.addCleanup(api_patch.stop)

    def assert_release(self):
        entries = list(GhostTelegramOutbox.objects.filter(chat_id=123).order_by('id'))
        self.assertEqual([entry.method for entry in entries], ['sendMessage', 'sendDocument'])
        text = entries[0].payload['text']
        self.assertIn('https://disk.yandex.ru/d/fixture-windows', text)
        self.assertIn('DeskNotes.exe', text)
        self.assertNotIn('Iu8xs_Fsy8o5Tg', text)
        self.assertNotIn('Edge.exe', text)
        self.assertEqual(entries[1].payload['document'], 'new-installer-fixture')
        self.assertIn('DeskNotes.exe', entries[1].payload['caption'])

    def test_trial_sends_configured_link_and_new_document(self):
        process_telegram_update({'update_id': 123456, 'message': {
            'chat': {'id': 123, 'type': 'private'}, 'text': '/trial'}})
        self.assert_release()

    def test_purchase_sends_same_release_and_does_not_duplicate_it(self):
        order = GhostPurchaseOrder.objects.create(customer_name='Fixture',
            customer_telegram='123', amount=1, starts_at=timezone.now(), duration_minutes=20)
        self.assertTrue(notify_purchase_token(order, token_value='FIXTURE'))
        self.assert_release()
        self.assertTrue(notify_purchase_token(order, token_value='FIXTURE'))
        self.assertEqual(GhostTelegramOutbox.objects.filter(chat_id=123).count(), 2)

    def test_email_and_all_instruction_links_use_configuration(self):
        text = purchase_tips.post_purchase_email_plain(customer_name='Fixture',
            token_value='FIXTURE', access_type='remote', starts_at='17.09.2026 20:00',
            duration_minutes=20)
        self.assertIn('https://disk.yandex.ru/d/fixture-windows', text)
        self.assertNotIn('Iu8xs_Fsy8o5Tg', text)
        self.assertNotIn('Edge.exe', text)
        self.assertIn('https://disk.yandex.ru/d/fixture-windows',
            purchase_tips.usage_instructions_telegram_html())

    def test_no_network_still_queues_new_installer(self):
        self.api.side_effect = RuntimeError('offline')
        process_telegram_update({'update_id': 123457, 'message': {
            'chat': {'id': 123, 'type': 'private'}, 'text': '/trial'}})
        self.assert_release()
        self.assertEqual(GhostTelegramOutbox.objects.filter(status='pending').count(), 2)
