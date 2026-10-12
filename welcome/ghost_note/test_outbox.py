from datetime import timedelta
from io import StringIO
from unittest.mock import patch, Mock

import requests
from django.contrib.admin.sites import AdminSite
from django.core.management import call_command
from django.db import transaction
from django.test import TransactionTestCase, override_settings, RequestFactory
from django.utils import timezone

from .models import GhostTelegramOutbox as O, GhostTelegramBotMessage as M, GhostTelegramContact as C, GhostAccessToken, GhostPurchaseOrder
from .outbox import (TelegramAPIError, deliver, enqueue, process_outbox, telegram_update_scope,
                     _claim, _record_success, safe_error)
from .telegram_notify import send_bot_message, send_bot_document, notify_trial_token, _call_telegram_api
from .trial import issue_trial_token


@override_settings(GHOST_NOTE_BOT_TOKEN='test-secret', GHOST_NOTE_ADMIN_CHAT_ID='', GHOST_NOTE_INSTALLER_FILE_ID='installer-id')
class OutboxTests(TransactionTestCase):
    def setUp(self):
        self.api = patch('ghost_note.telegram_notify._call_telegram_api', return_value={'ok': True, 'result': {'message_id': 1}}).start()
        self.addCleanup(patch.stopall)

    def due(self, item):
        O.objects.filter(pk=item.pk).update(next_attempt_at=timezone.now()-timedelta(seconds=1))

    def message(self, chat=123, text='hello', **kwargs):
        return enqueue(chat_id=chat, method='sendMessage', payload={'chat_id': chat, 'text': text}, **kwargs)

    def test_sent_only_once_and_log_is_actual_sent_message(self):
        result=send_bot_message(chat_id=123, text='hello', idempotency_key='one')
        repeated=send_bot_message(chat_id=123, text='hello', idempotency_key='one')
        self.assertEqual(result['status'], O.Status.SENT)
        self.assertEqual(repeated['outbox_id'], result['outbox_id'])
        self.assertEqual(self.api.call_count, 1)
        self.assertEqual(M.objects.get().text, 'hello')
        self.assertFalse(result['queued'])

    def test_connection_timeout_retried_from_database_without_command(self):
        self.api.side_effect=RuntimeError('ConnectTimeout')
        result=send_bot_message(chat_id=123,text='saved',reply_markup={'inline_keyboard': [[{'text':'Да','callback_data':'trial_yes'}]]})
        self.assertTrue(result['queued'])
        self.assertEqual(M.objects.count(),0)
        item=O.objects.get(); self.assertEqual(item.attempts,1)
        self.assertGreater(item.next_attempt_at, timezone.now())
        self.api.side_effect=None; self.due(item)
        with patch('ghost_note.telegram_bot.process_telegram_update') as handler:
            self.assertEqual(process_outbox()['sent'],1)
            handler.assert_not_called()
        item.refresh_from_db(); self.assertEqual(item.attempts,2)
        self.assertIn('reply_markup',self.api.call_args.args[1])

    def test_transaction_rollback_sends_and_keeps_nothing(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                send_bot_message(chat_id=123,text='rollback')
                self.api.assert_not_called()
                raise RuntimeError('rollback')
        self.assertEqual(O.objects.count(),0)
        self.api.assert_not_called()

    def test_transaction_commit_sends_after_commit(self):
        with transaction.atomic():
            result=send_bot_message(chat_id=123,text='committed')
            self.assertTrue(result['queued']); self.api.assert_not_called()
        self.api.assert_called_once()
        self.assertEqual(O.objects.get().status,O.Status.SENT)

    def test_per_chat_order_does_not_block_other_chats(self):
        self.api.side_effect=RuntimeError('offline')
        send_bot_message(chat_id=123,text='first')
        send_bot_document(chat_id=123,file_id='installer-id')
        self.assertEqual(self.api.call_count,1)
        self.api.side_effect=None
        send_bot_message(chat_id=456,text='other chat')
        self.assertEqual(self.api.call_count,2)
        self.due(O.objects.get(chat_id=123,method='sendMessage'))
        self.assertEqual(process_outbox()['sent'],2)
        self.assertEqual([call.args[0] for call in self.api.call_args_list[-2:]],['sendMessage','sendDocument'])

    def test_no_resend_while_leased_and_recover_expired_lease(self):
        item=self.message(); claimed=_claim(item.pk,timezone.now())
        self.assertIsNotNone(claimed)
        self.assertEqual(deliver(item.pk),'skipped'); self.api.assert_not_called()
        self.due(item)
        self.assertEqual(deliver(item.pk),'sent')
        self.assertEqual(_record_success(claimed),'skipped')
        self.assertEqual(M.objects.count(),1)

    def test_nested_worker_cannot_send_same_message(self):
        item=self.message()
        def sending(*args,**kwargs):
            self.assertEqual(deliver(item.pk),'skipped')
            return {'ok':True}
        self.api.side_effect=sending
        self.assertEqual(deliver(item.pk),'sent'); self.assertEqual(self.api.call_count,1)

    def test_update_replay_deduplicates_but_new_command_is_allowed(self):
        for update in (55,55,56):
            with telegram_update_scope(update):
                send_bot_message(chat_id=123,text='same')
        self.assertEqual(O.objects.count(),2)
        self.assertEqual(self.api.call_count,2)

    def test_flood_limit_respects_retry_after(self):
        self.api.side_effect=TelegramAPIError('Too Many Requests',429,120)
        send_bot_message(chat_id=123,text='limit'); item=O.objects.get()
        self.assertEqual(item.status,O.Status.PENDING)
        self.assertGreaterEqual((item.next_attempt_at-timezone.now()).total_seconds(),119)

    def test_permanent_error_stops_retries(self):
        self.api.side_effect=TelegramAPIError('Forbidden: bot was blocked by the user',403)
        send_bot_message(chat_id=123,text='blocked'); item=O.objects.get()
        self.assertEqual(item.status,O.Status.FAILED)
        self.due(item); self.assertEqual(process_outbox()['sent'],0)
        self.assertEqual(self.api.call_count,1)

    def test_temporary_file_error_is_retried(self):
        self.api.side_effect=TelegramAPIError('file is temporarily unavailable',400)
        send_bot_document(chat_id=123,file_id='installer-id')
        self.assertEqual(O.objects.get().status,O.Status.PENDING)

    @override_settings(GHOST_NOTE_ADMIN_CHAT_ID=999)
    def test_alert_after_six_failures_only_once_without_recursion(self):
        self.api.side_effect=RuntimeError('offline')
        send_bot_message(chat_id=123,text='waiting'); item=O.objects.get(chat_id=123)
        for attempt in range(2,8):
            self.due(item); deliver(item.pk)
        alerts=O.objects.filter(is_alert=True)
        self.assertEqual(alerts.count(),1)
        self.assertIn('Попыток: 6',alerts.get().payload['text'])
        alert=alerts.get()
        for _ in range(7):
            self.due(alert); deliver(alert.pk)
        self.assertEqual(alerts.count(),1)

    def test_long_message_resumes_failed_part_and_keyboard_last(self):
        self.api.side_effect=[{'ok':True},RuntimeError('offline')]
        result=send_bot_message(chat_id=123,text='<b>'+('a'*7000)+'</b>',reply_markup={'inline_keyboard':[]})
        item=O.objects.get(pk=result['outbox_id'])
        self.assertEqual(item.next_part,1)
        self.assertNotIn('reply_markup',self.api.call_args_list[0].args[1])
        self.assertIn('reply_markup',self.api.call_args_list[1].args[1])
        failed_payload=self.api.call_args_list[1].args[1]
        self.api.side_effect=None; self.due(item); deliver(item.pk)
        self.assertEqual(self.api.call_count,3)
        self.assertEqual(self.api.call_args.args[1],failed_payload)
        self.assertEqual(M.objects.count(),1)

    def test_trial_queues_token_atomically_and_retry_never_issues_another(self):
        self.api.side_effect=RuntimeError('offline')
        C.objects.create(telegram_user_id=123,username='tester')
        token=issue_trial_token(telegram_user_id=123,username='tester')
        item=O.objects.get(idempotency_key='trial-token:{}'.format(token.pk))
        self.assertIn(token.token,item.payload['text'])
        self.assertIsNotNone(C.objects.get().last_trial_at)
        self.api.side_effect=None; self.due(item); process_outbox()
        notify_trial_token(chat_id=123,token=token)
        self.assertEqual(GhostAccessToken.objects.count(),1)
        self.assertEqual(O.objects.filter(chat_id=123).count(),1)
        self.assertEqual(M.objects.filter(telegram_user_id=123).count(),1)

    def test_trial_rollback_if_outbox_cannot_be_persisted(self):
        C.objects.create(telegram_user_id=123)
        with patch('ghost_note.outbox.enqueue',side_effect=RuntimeError('db failure')):
            with self.assertRaises(RuntimeError):
                issue_trial_token(telegram_user_id=123)
        self.assertEqual(GhostAccessToken.objects.count(),0)
        self.assertIsNone(C.objects.get().last_trial_at)
        self.api.assert_not_called()

    def test_trial_instruction_failure_still_queues_installer(self):
        from .telegram_bot import process_telegram_update
        self.api.side_effect=RuntimeError('offline')
        C.objects.create(telegram_user_id=123)
        process_telegram_update({'update_id':100,'message':{'chat':{'id':123},'text':'/trial'}})
        self.assertEqual(list(O.objects.order_by('id').values_list('method',flat=True)),['sendMessage','sendDocument'])
        self.assertEqual(self.api.call_count,1)
        self.assertEqual(GhostAccessToken.objects.count(),0)

    def test_purchase_timestamp_written_only_when_delivered(self):
        order=GhostPurchaseOrder.objects.create(customer_name='Test',amount=1,starts_at=timezone.now(),duration_minutes=20)
        self.api.side_effect=RuntimeError('offline')
        send_bot_message(chat_id=123,text='token',completion={'purchase_order':order.pk})
        order.refresh_from_db(); self.assertIsNone(order.telegram_notified_at)
        self.assertEqual(order.telegram_notify_error,'offline')
        self.api.side_effect=None; item=O.objects.get(); self.due(item); deliver(item.pk)
        order.refresh_from_db(); self.assertIsNotNone(order.telegram_notified_at)
        self.assertEqual(order.telegram_notify_error,'')

    def test_polling_restart_does_not_drop_commands(self):
        from .management.commands.ghost_telegram_poll import Command
        with patch('ghost_note.management.commands.ghost_telegram_poll.delete_telegram_webhook') as delete, patch('ghost_note.management.commands.ghost_telegram_poll.get_telegram_updates',side_effect=KeyboardInterrupt):
            Command(stdout=StringIO()).handle()
        delete.assert_called_once_with(drop_pending_updates=False)

    def test_api_rate_limit_metadata_and_token_redaction(self):
        # Call the imported original function while patching only its HTTP transport.
        response=Mock(status_code=429)
        response.json.return_value={'ok':False,'description':'Too Many Requests','error_code':429,'parameters':{'retry_after':32}}
        with patch('ghost_note.telegram_notify.TELEGRAM_SESSION.post',return_value=response):
            with self.assertRaises(TelegramAPIError) as raised:
                _call_telegram_api('sendMessage',{})
        self.assertEqual(raised.exception.retry_after,32)
        with patch('ghost_note.telegram_notify._endpointBaseToRetryAt', {}), patch('ghost_note.telegram_notify.TELEGRAM_SESSION.post',side_effect=requests.ConnectTimeout('https://api.telegram.org/bottest-secret/sendMessage')):
            with self.assertRaises(RuntimeError) as raised:
                _call_telegram_api('sendMessage',{})
        self.assertNotIn('test-secret',str(raised.exception))
        self.assertNotIn('test-secret',safe_error('test-secret'))

    def test_admin_pending_does_not_echo_previous_sent_message(self):
        from .admin import GhostTelegramBotMessageAdmin
        M.objects.create(telegram_user_id=123,direction='out',text='older response')
        C.objects.create(telegram_user_id=123)
        self.api.side_effect=RuntimeError('offline')
        request=RequestFactory().post('/',{'telegram_user_id':'123','text':'new message'})
        request.user=Mock(is_active=True,is_staff=True,is_superuser=True)
        response=GhostTelegramBotMessageAdmin(M,AdminSite()).send_message_view(request)
        import json
        data=json.loads(response.content)
        self.assertTrue(data['queued']); self.assertEqual(data['message_html'],'')

    def test_worker_command_drains_due_messages(self):
        self.message(); self.message(chat=456)
        output=StringIO(); call_command('process_ghost_outbox',stdout=output)
        self.assertEqual(O.objects.filter(status=O.Status.SENT).count(),2)

    def test_database_failure_after_document_send_does_not_resend_document(self):
        with patch('ghost_note.outbox._record_success',side_effect=RuntimeError('db lost')):
            result=send_bot_document(chat_id=123,file_id='installer-id')
        item=O.objects.get(pk=result['outbox_id'])
        self.assertEqual(item.next_part,1)
        self.assertEqual(M.objects.count(),0)
        self.due(item); deliver(item.pk)
        self.assertEqual(self.api.call_count,1)
        self.assertEqual(M.objects.count(),1)

    def test_failed_purchase_enqueue_can_resume_without_duplicate_token_text(self):
        from .telegram_notify import notify_purchase_token
        order=GhostPurchaseOrder.objects.create(customer_name='Test',customer_telegram='123',
            amount=1,starts_at=timezone.now(),duration_minutes=20)
        self.api.side_effect=RuntimeError('offline')
        notify_purchase_token(order,token_value='ABCDEF')
        notify_purchase_token(order,token_value='ABCDEF')
        self.assertEqual(O.objects.filter(chat_id=123).count(),2)
        self.api.side_effect=None
        self.due(O.objects.get(method='sendMessage'))
        self.assertEqual(process_outbox()['sent'],2)
        order.refresh_from_db(); self.assertIsNotNone(order.telegram_notified_at)

    def test_backoff_grows_and_is_capped(self):
        item=self.message()
        self.api.side_effect=RuntimeError('offline')
        for attempts,minimum,maximum in [(1,4,6),(2,9,11),(3,19,21),(12,3599,3601)]:
            O.objects.filter(pk=item.pk).update(attempts=attempts-1)
            self.due(item); deliver(item.pk); item.refresh_from_db()
            delay=(item.next_attempt_at-timezone.now()).total_seconds()
            self.assertGreaterEqual(delay,minimum); self.assertLess(delay,maximum)
