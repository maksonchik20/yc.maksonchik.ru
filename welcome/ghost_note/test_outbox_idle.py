import os
import tempfile
from datetime import timedelta
from io import StringIO
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.db import InterfaceError, OperationalError, connection, transaction
from django.test import SimpleTestCase, TransactionTestCase
from django.utils import timezone

from .models import GhostTelegramOutbox as Outbox
from .outbox import _claim, deliver, enqueue, next_outbox_attempt, process_outbox
from .outbox_wake import notify_outbox_worker, read_wake_version, wait_for_outbox_work
from .telegram_notify import send_bot_message


class FakeStopEvent:
    def __init__(self):
        self.elapsed = 0
        self.stopped = False
        self.waits = []
        self.on_wait = None

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.waits.append(seconds)
        self.elapsed += seconds
        if self.on_wait:
            self.on_wait()


class WakeFileTests(SimpleTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = os.path.join(temporary.name, 'wake')
        self.override = self.settings(GHOST_OUTBOX_WAKE_PATH=self.path)
        self.override.enable()
        self.addCleanup(self.override.disable)

    def wait(self, stop, version, next_attempt_at=None):
        with patch('ghost_note.outbox_wake.time.monotonic', side_effect=lambda: stop.elapsed):
            wait_for_outbox_work(stop, version, next_attempt_at)

    def test_atomic_notifications_change_version_without_leaving_temporary_files(self):
        self.assertIsNone(read_wake_version())
        notify_outbox_worker()
        first = read_wake_version()
        notify_outbox_worker()
        self.assertNotEqual(first, read_wake_version())
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ['wake'])
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_missing_runtime_directory_does_not_break_delivery(self):
        with self.settings(GHOST_OUTBOX_WAKE_PATH=os.path.join(self.path, 'absent', 'wake')):
            notify_outbox_worker()
            self.assertIsNone(read_wake_version())

    def test_failed_notification_cleanup_does_not_raise(self):
        with patch('ghost_note.outbox_wake.os.replace', side_effect=OSError('disk full')):
            notify_outbox_worker()
        self.assertEqual(os.listdir(os.path.dirname(self.path)), [])

    def test_idle_waits_without_sql_until_safety_scan(self):
        stop = FakeStopEvent()
        self.wait(stop, None)
        self.assertEqual(stop.elapsed, 30)
        self.assertEqual(stop.waits, [1.0] * 30)

    def test_wake_from_another_process_is_observed_within_one_second(self):
        stop = FakeStopEvent()
        stop.on_wait = notify_outbox_worker
        self.wait(stop, None)
        self.assertEqual(stop.elapsed, 1)

    def test_wake_during_scan_is_not_lost_before_wait(self):
        observed = read_wake_version()
        notify_outbox_worker()
        stop = FakeStopEvent()
        self.wait(stop, observed)
        self.assertEqual(stop.elapsed, 0)

    def test_retries_wake_at_due_time_not_idle_timeout(self):
        now = timezone.now()
        stop = FakeStopEvent()
        with patch('ghost_note.outbox_wake.timezone.now', return_value=now):
            self.wait(stop, None, now + timedelta(seconds=2.5))
        self.assertEqual(stop.waits, [1.0, 1.0, 0.5])

    def test_overdue_retry_does_not_wait(self):
        stop = FakeStopEvent()
        self.wait(stop, None, timezone.now() - timedelta(seconds=1))
        self.assertEqual(stop.waits, [])

    def test_stop_interrupts_idle_wait(self):
        stop = FakeStopEvent()
        stop.on_wait = stop.set
        self.wait(stop, None)
        self.assertEqual(stop.elapsed, 1)


class IdleOutboxDatabaseTests(TransactionTestCase):
    def setUp(self):
        self.api = patch('ghost_note.telegram_notify._call_telegram_api', return_value={'ok': True}).start()
        self.notify = patch('ghost_note.outbox.notify_outbox_worker').start()
        self.addCleanup(patch.stopall)

    def message(self, chat_id=123):
        return enqueue(chat_id=chat_id, method='sendMessage',
                       payload={'chat_id': chat_id, 'text': 'test'})

    def test_enqueue_wakes_only_after_commit_and_never_on_rollback(self):
        with transaction.atomic():
            self.message()
            self.notify.assert_not_called()
        self.notify.assert_called_once_with()
        self.notify.reset_mock()
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.message()
                raise RuntimeError('rollback')
        self.notify.assert_not_called()
        self.assertEqual(Outbox.objects.count(), 1)

    def test_command_fast_path_still_sends_immediately_without_worker(self):
        with self.settings(GHOST_NOTE_BOT_TOKEN='test'):
            with patch('ghost_note.outbox.process_outbox') as background:
                result = send_bot_message(chat_id=123, text='immediate command reply')
        self.api.assert_called_once()
        background.assert_not_called()
        self.assertEqual(result['status'], Outbox.Status.SENT)

    def test_start_and_trial_replies_do_not_wait_for_background_worker(self):
        from .telegram_bot import process_telegram_update
        with self.settings(GHOST_NOTE_BOT_TOKEN='test', GHOST_NOTE_ADMIN_CHAT_ID='',
                           GHOST_NOTE_INSTALLER_FILE_ID='test-installer'):
            with patch('ghost_note.outbox.process_outbox') as background:
                for update_id, command in enumerate(('/start', '/trial'), start=100):
                    before = self.api.call_count
                    process_telegram_update({
                        'update_id': update_id,
                        'message': {'chat': {'id': update_id}, 'text': command},
                    })
                    self.assertGreater(self.api.call_count, before, command)
                background.assert_not_called()
        self.assertFalse(Outbox.objects.filter(status=Outbox.Status.PENDING).exists())

    def test_retry_and_completion_notify_other_worker_after_commit(self):
        item = self.message()
        self.notify.reset_mock()
        self.api.side_effect = RuntimeError('offline')
        self.assertEqual(deliver(item.pk), 'retry')
        self.notify.assert_called_once_with()
        self.assertIsNotNone(next_outbox_attempt())
        self.notify.reset_mock()
        Outbox.objects.filter(pk=item.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        self.api.side_effect = None
        self.assertEqual(deliver(item.pk), 'sent')
        self.notify.assert_called_once_with()
        self.assertIsNone(next_outbox_attempt())

    def test_deadline_ignores_due_followup_blocked_by_earlier_lease(self):
        first = self.message()
        _claim(first.pk, timezone.now())
        self.message()
        first.refresh_from_db()
        self.assertEqual(next_outbox_attempt(), first.next_attempt_at)
        other = self.message(chat_id=456)
        self.assertEqual(next_outbox_attempt(), other.next_attempt_at)

    def test_worker_restart_recovers_database_task_even_if_wakeup_was_lost(self):
        self.message()
        stop = FakeStopEvent()
        with patch('ghost_note.management.commands.process_ghost_outbox.threading.Event', return_value=stop), \
                patch('ghost_note.management.commands.process_ghost_outbox.signal.signal'), \
                patch('ghost_note.management.commands.process_ghost_outbox.read_wake_version', return_value=None), \
                patch('ghost_note.management.commands.process_ghost_outbox.wait_for_outbox_work', side_effect=lambda *args, **kwargs: stop.set()):
            call_command('process_ghost_outbox', loop=True, stdout=StringIO())
        self.assertEqual(Outbox.objects.get().status, Outbox.Status.SENT)

    def test_queued_only_message_wakes_idle_worker_within_one_second(self):
        stop = FakeStopEvent()

        def enqueue_during_idle():
            self.message()
            stop.on_wait = None

        def mark_delivery(*args, **kwargs):
            stop.set()
            return {'ok': True}

        stop.on_wait = enqueue_during_idle
        self.api.side_effect = mark_delivery
        with tempfile.TemporaryDirectory() as runtime:
            with self.settings(GHOST_OUTBOX_WAKE_PATH=os.path.join(runtime, 'wake')), \
                    patch('ghost_note.outbox.notify_outbox_worker', side_effect=notify_outbox_worker), \
                    patch('ghost_note.management.commands.process_ghost_outbox.threading.Event', return_value=stop), \
                    patch('ghost_note.management.commands.process_ghost_outbox.signal.signal'), \
                    patch('ghost_note.outbox_wake.time.monotonic', side_effect=lambda: stop.elapsed):
                call_command('process_ghost_outbox', loop=True, stdout=StringIO())
        self.api.assert_called_once()
        self.assertEqual(stop.elapsed, 1)
        self.assertEqual(Outbox.objects.get().status, Outbox.Status.SENT)

    def test_crashed_sender_lease_is_recovered_without_replaying_command(self):
        item = self.message()
        _claim(item.pk, timezone.now())
        self.assertEqual(process_outbox()['sent'], 0)
        Outbox.objects.filter(pk=item.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(process_outbox()['sent'], 1)
        self.api.assert_called_once()

    def test_empty_queue_uses_two_queries_per_scan_and_none_while_waiting(self):
        with self.assertNumQueries(2):
            process_outbox()
            due = next_outbox_attempt()
        self.assertIsNone(due)
        stop = FakeStopEvent()
        with patch('ghost_note.outbox_wake.time.monotonic', side_effect=lambda: stop.elapsed), \
                patch('ghost_note.outbox_wake.read_wake_version', return_value=None):
            with self.assertNumQueries(0):
                wait_for_outbox_work(stop, None, due)
        self.assertEqual(stop.elapsed, 30)

    def test_dead_connection_is_closed_before_batch_and_does_not_drop_work(self):
        stop = FakeStopEvent()
        fake_connection = Mock(connection=object(), settings_dict={'CONN_MAX_AGE': 0})
        fake_connection.is_usable.return_value = False
        with patch('ghost_note.management.commands.process_ghost_outbox.threading.Event', return_value=stop), \
                patch('ghost_note.management.commands.process_ghost_outbox.signal.signal'), \
                patch('ghost_note.management.commands.process_ghost_outbox.connection', fake_connection), \
                patch('ghost_note.management.commands.process_ghost_outbox.close_old_connections'), \
                patch('ghost_note.management.commands.process_ghost_outbox.process_outbox', return_value={}), \
                patch('ghost_note.management.commands.process_ghost_outbox.next_outbox_attempt', return_value=None), \
                patch('ghost_note.management.commands.process_ghost_outbox.wait_for_outbox_work', side_effect=lambda *args, **kwargs: stop.set()):
            call_command('process_ghost_outbox', loop=True, stdout=StringIO())
        self.assertEqual(fake_connection.close.call_count, 2)
        self.assertEqual(fake_connection.settings_dict['CONN_MAX_AGE'], 0)

    def test_minute_of_idle_worker_scans_twice_instead_of_thirty_times(self):
        stop = FakeStopEvent()
        stop.on_wait = lambda: stop.set() if stop.elapsed >= 60 else None
        old_max_age = connection.settings_dict.get('CONN_MAX_AGE', 0)
        with patch('ghost_note.management.commands.process_ghost_outbox.threading.Event', return_value=stop), \
                patch('ghost_note.management.commands.process_ghost_outbox.signal.signal'), \
                patch('ghost_note.management.commands.process_ghost_outbox.read_wake_version', return_value=None), \
                patch('ghost_note.outbox_wake.read_wake_version', return_value=None), \
                patch('ghost_note.outbox_wake.time.monotonic', side_effect=lambda: stop.elapsed), \
                patch('ghost_note.management.commands.process_ghost_outbox.connection.is_usable', return_value=True):
            with self.assertNumQueries(4):
                call_command('process_ghost_outbox', loop=True, stdout=StringIO())
        self.assertEqual(connection.settings_dict['CONN_MAX_AGE'], old_max_age)
        self.assertEqual(stop.elapsed, 60)

    def test_scoped_connection_reuse_and_database_failure_retry(self):
        stop = FakeStopEvent()
        fake_connection = Mock(connection=None, settings_dict={'CONN_MAX_AGE': 0})

        def fail_batch(**kwargs):
            self.assertEqual(fake_connection.settings_dict['CONN_MAX_AGE'], 60)
            raise RuntimeError('database unavailable')

        with patch('ghost_note.management.commands.process_ghost_outbox.threading.Event', return_value=stop), \
                patch('ghost_note.management.commands.process_ghost_outbox.signal.signal'), \
                patch('ghost_note.management.commands.process_ghost_outbox.connection', fake_connection), \
                patch('ghost_note.management.commands.process_ghost_outbox.close_old_connections'), \
                patch('ghost_note.management.commands.process_ghost_outbox.process_outbox', side_effect=fail_batch), \
                patch('ghost_note.management.commands.process_ghost_outbox.wait_for_outbox_work', side_effect=lambda *args, **kwargs: stop.set()) as wait:
            with self.assertLogs('ghost_note.management.commands.process_ghost_outbox', level='ERROR'):
                call_command('process_ghost_outbox', loop=True, stdout=StringIO())
        deadline = wait.call_args.args[2]
        self.assertLessEqual((deadline - timezone.now()).total_seconds(), 1)
        self.assertGreater((deadline - timezone.now()).total_seconds(), 0)
        self.assertEqual(fake_connection.settings_dict['CONN_MAX_AGE'], 0)

    def test_native_close_error_recovers_in_second_worker_batch(self):
        self._check_close_error_recovery(expired=False)

    def test_expired_stale_close_error_recovers_in_second_worker_batch(self):
        self._check_close_error_recovery(expired=True)

    def _check_close_error_recovery(self, expired):
        stop = FakeStopEvent()
        fake_connection = Mock(connection=None, settings_dict={'CONN_MAX_AGE': 0})
        fake_connection.is_usable.return_value = False
        batch_calls = []
        wait_calls = []

        def native_close():
            if fake_connection.connection is not None:
                fake_connection.connection = None
                raise OperationalError(2006, 'MySQL server has gone away')

        def close_old():
            # First post-batch cleanup still sees an unexpired handle. The
            # second iteration sees an expired one and closes it before ping.
            if expired and wait_calls and fake_connection.connection is not None:
                native_close()

        def batch(**kwargs):
            self.assertIsNone(fake_connection.connection)
            batch_calls.append(1)
            fake_connection.connection = object()
            return {}

        def wait(*args, **kwargs):
            wait_calls.append(1)
            if len(wait_calls) == 2:
                stop.set()

        fake_connection.close.side_effect = native_close
        with patch('ghost_note.management.commands.process_ghost_outbox.threading.Event', return_value=stop), \
                patch('ghost_note.management.commands.process_ghost_outbox.signal.signal'), \
                patch('ghost_note.management.commands.process_ghost_outbox.connection', fake_connection), \
                patch('ghost_note.management.commands.process_ghost_outbox.close_old_connections', side_effect=close_old), \
                patch('ghost_note.management.commands.process_ghost_outbox.process_outbox', side_effect=batch), \
                patch('ghost_note.management.commands.process_ghost_outbox.next_outbox_attempt', return_value=None), \
                patch('ghost_note.management.commands.process_ghost_outbox.wait_for_outbox_work', side_effect=wait):
            call_command('process_ghost_outbox', loop=True, stdout=StringIO())
        self.assertEqual(len(batch_calls), 2)
        self.assertEqual(len(wait_calls), 2)
        self.assertIsNone(fake_connection.connection)
        self.assertEqual(fake_connection.settings_dict['CONN_MAX_AGE'], 0)

    def test_close_guard_does_not_hide_unrelated_error_or_uncleared_connection(self):
        from .management.commands.process_ghost_outbox import _close_connections_safely
        for code, clear_connection in ((2006, False), (1045, True)):
            fake_connection = Mock(connection=object())

            def failing_close():
                if clear_connection:
                    fake_connection.connection = None
                raise OperationalError(code, 'unexpected close failure')

            with patch('ghost_note.management.commands.process_ghost_outbox.connection', fake_connection):
                with self.assertRaises(OperationalError):
                    _close_connections_safely(failing_close)

    def test_close_guard_accepts_closed_interface_error(self):
        from .management.commands.process_ghost_outbox import _close_connections_safely
        fake_connection = Mock(connection=object())

        def disconnected_close():
            fake_connection.connection = None
            raise InterfaceError(0, '')

        with patch('ghost_note.management.commands.process_ghost_outbox.connection', fake_connection):
            _close_connections_safely(disconnected_close)
