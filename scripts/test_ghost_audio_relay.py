"""Isolated socket/wire regression tests; never load production Django or a DB.

Run with the relay venv: python -m unittest discover -s scripts -p test_ghost_audio_relay.py
"""
import asyncio
import importlib.util
import os
from pathlib import Path
import socket
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import websockets
from websockets.exceptions import ConnectionClosed


def load_relay():
    def async_wrapper(function, **_options):
        async def wrapped(*args, **kwargs):
            return function(*args, **kwargs)
        return wrapped

    moduleNameToModule = {}
    for name in ('django', 'django.db', 'asgiref', 'asgiref.sync', 'ghost_note',
                 'ghost_note.auth', 'ghost_note.models', 'ghost_note.security_v2'):
        moduleNameToModule[name] = types.ModuleType(name)
    moduleNameToModule['django'].setup = lambda: None
    moduleNameToModule['django.db'].DatabaseError = type('DatabaseError', (Exception,), {})
    moduleNameToModule['django.db'].InterfaceError = type('InterfaceError', (Exception,), {})
    moduleNameToModule['django.db'].close_old_connections = MagicMock()
    moduleNameToModule['django.db'].connection = MagicMock()
    moduleNameToModule['asgiref.sync'].sync_to_async = async_wrapper
    moduleNameToModule['ghost_note.auth'].session_token_valid = MagicMock()
    moduleNameToModule['ghost_note.auth'].validate_access_token = MagicMock()
    moduleNameToModule['ghost_note.models'].GhostSession = MagicMock()
    moduleNameToModule['ghost_note.security_v2'].SecurityError = ValueError
    moduleNameToModule['ghost_note.security_v2'].require_session = MagicMock()
    moduleNameToModule['ghost_note.security_v2'].session_is_v2 = MagicMock()
    spec = importlib.util.spec_from_file_location(
        'isolated_ghost_audio_relay', Path(__file__).with_name('ghost_audio_relay.py'))
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, moduleNameToModule), patch.dict(os.environ):
        spec.loader.exec_module(module)
    return module


relay = load_relay()


class ListenerEnvironmentTests(unittest.TestCase):
    def test_manual_mode_has_no_inherited_listener(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(relay.inherited_listener())

    def test_wrong_pid_or_multiple_descriptors_fail_closed(self):
        for pid, count in (('0', '1'), (str(os.getpid()), '2')):
            with self.subTest(pid=pid, count=count), patch.dict(
                    os.environ, {'LISTEN_PID': pid, 'LISTEN_FDS': count}, clear=True):
                with self.assertRaises(RuntimeError):
                    relay.inherited_listener()

    def test_systemd_fd_is_consumed_noninheritable_without_rebinding(self):
        listener = MagicMock()
        listener.family = socket.AF_INET
        listener.type = socket.SOCK_STREAM
        listener.getsockopt.return_value = 1
        with patch.dict(os.environ, {'LISTEN_PID': str(os.getpid()), 'LISTEN_FDS': '1'},
                        clear=True), patch.object(relay.socket, 'socket', return_value=listener) as factory:
            self.assertIs(relay.inherited_listener(), listener)
            factory.assert_called_once_with(fileno=3)
            listener.set_inheritable.assert_called_once_with(False)
            listener.setblocking.assert_called_once_with(False)
            self.assertNotIn('LISTEN_FDS', os.environ)

    def test_session_lookup_does_not_load_screenshot(self):
        model = MagicMock()
        model.DoesNotExist = type('DoesNotExist', (Exception,), {})
        with patch.object(relay, 'GhostSession', model):
            self.assertIs(relay._get_session('session'), model.objects.defer.return_value.get.return_value)
            model.objects.defer.assert_called_once_with('screenshot')
            model.objects.defer.return_value.get.assert_called_once_with(session_id='session')


class DatabaseLifecycleTests(unittest.TestCase):
    def test_stale_connection_is_closed_before_auth_and_http_policy_restored(self):
        settingToValue = {'CONN_MAX_AGE': 0}
        database = MagicMock(connection=object(), settings_dict=settingToValue)
        database.is_usable.return_value = False

        def query(argument):
            database.close.assert_called_once_with()
            self.assertEqual(database.settings_dict['CONN_MAX_AGE'], 60)
            self.assertIsNot(database.settings_dict, settingToValue)
            return argument

        with patch.object(relay, 'connection', database), patch.object(relay, 'close_old_connections') as cleanup:
            self.assertEqual(relay.database_call(query, 'session'), 'session')
            self.assertEqual(cleanup.call_count, 2)
        self.assertIs(database.settings_dict, settingToValue)
        self.assertEqual(settingToValue['CONN_MAX_AGE'], 0)

    def test_healthy_connection_reused_and_db_failures_close_before_propagating(self):
        settingToValue = {'CONN_MAX_AGE': 0}
        database = MagicMock(connection=object(), settings_dict=settingToValue)
        database.is_usable.return_value = True
        query = MagicMock(return_value='valid')
        with patch.object(relay, 'connection', database), patch.object(relay, 'close_old_connections'):
            self.assertEqual(relay.database_call(query), 'valid')
            database.close.assert_not_called()
            query.side_effect = relay.DatabaseError('connection dropped during query')
            with self.assertRaises(relay.DatabaseError):
                relay.database_call(query)
            database.close.assert_called_once_with()
        self.assertIs(database.settings_dict, settingToValue)

    def test_failed_healthcheck_reconnects_before_callback(self):
        database = MagicMock(connection=object(), settings_dict={'CONN_MAX_AGE': 0})
        database.is_usable.side_effect = relay.InterfaceError('closed')
        with patch.object(relay, 'connection', database), patch.object(relay, 'close_old_connections'):
            relay.database_call(lambda: database.close.assert_called_once_with())

    def test_idle_expiry_cleanup_is_called_before_healthcheck(self):
        database = MagicMock(connection=object(), settings_dict={'CONN_MAX_AGE': 0})

        def cleanup():
            database.connection = None

        with patch.object(relay, 'connection', database), patch.object(relay, 'close_old_connections', side_effect=cleanup):
            relay.database_call(lambda: 'result')
            database.is_usable.assert_not_called()

    def test_shutdown_closes_database_explicitly(self):
        database = MagicMock()
        with patch.object(relay, 'connection', database):
            relay._close_database_connection()
            database.close.assert_called_once_with()

    def test_dead_handle_close_error_does_not_fail_next_auth(self):
        database = MagicMock(connection=object(), settings_dict={'CONN_MAX_AGE': 0})
        database.is_usable.return_value = False

        def close_dead_handle():
            database.connection = None
            raise relay.DatabaseError(2006, 'MySQL server has gone away')

        database.close.side_effect = close_dead_handle
        query = MagicMock(return_value='authorized by fresh query')
        with patch.object(relay, 'connection', database), patch.object(relay, 'close_old_connections'):
            self.assertEqual(relay.database_call(query), 'authorized by fresh query')
            query.assert_called_once_with()

    def test_old_connection_cleanup_error_is_ignored_only_after_handle_cleared(self):
        database = MagicMock(connection=object(), settings_dict={'CONN_MAX_AGE': 0})

        def close_dead_handle():
            database.connection = None
            raise relay.InterfaceError('already closed')

        query = MagicMock(return_value='authorized')
        with patch.object(relay, 'connection', database), patch.object(
                relay, 'close_old_connections', side_effect=close_dead_handle):
            self.assertEqual(relay.database_call(query), 'authorized')
            query.assert_called_once_with()

    def test_shutdown_close_error_is_ignored_after_handle_cleared(self):
        database = MagicMock(connection=object())

        def close_dead_handle():
            database.connection = None
            raise relay.DatabaseError(2006, 'already closed')

        database.close.side_effect = close_dead_handle
        with patch.object(relay, 'connection', database):
            relay._close_database_connection()
            database.close.assert_called_once_with()

    def test_failed_discard_with_handle_retained_never_authenticates(self):
        database = MagicMock(connection=object(), settings_dict={'CONN_MAX_AGE': 0})
        database.is_usable.return_value = False
        database.close.side_effect = relay.DatabaseError('cannot discard handle')
        query = MagicMock(return_value='must not authenticate')
        with patch.object(relay, 'connection', database), patch.object(relay, 'close_old_connections'):
            with self.assertRaises(relay.DatabaseError):
                relay.database_call(query)
            query.assert_not_called()


class RelaySocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        relay.sessionIdToRoom.clear()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(128)
        self.listener.setblocking(False)
        self.url = 'ws://127.0.0.1:%d/' % self.listener.getsockname()[1]
        self.tasks = []
        self.listener_auth = AsyncMock(return_value=(object(), None))
        self.uploader_auth = AsyncMock(return_value=(object(), None))
        self.patches = [patch.object(relay, 'validate_listener', self.listener_auth),
                        patch.object(relay, 'validate_uploader', self.uploader_auth),
                        patch.object(relay, 'AUTH_RECHECK_SECONDS', .03)]
        for active_patch in self.patches:
            active_patch.start()

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.listener.close()
        for active_patch in reversed(self.patches):
            active_patch.stop()
        self.assertFalse(relay.sessionIdToRoom)
        self.assertFalse(relay.fanout_tasks)

    def start_relay(self, idle_seconds=.1):
        task = asyncio.create_task(relay.serve_listener(self.listener, idle_seconds=idle_seconds))
        self.tasks.append(task)
        return task

    async def test_cold_start_keeps_backlog_and_concurrent_first_connections(self):
        # Both clients connect while only the systemd-like anchor socket exists.
        first = asyncio.ensure_future(websockets.connect(self.url + '?session_id=first&role=listen'))
        second = asyncio.ensure_future(websockets.connect(self.url + '?session_id=second&role=listen'))
        await asyncio.sleep(.03)
        self.assertFalse(first.done())
        self.assertFalse(second.done())
        relay_task = self.start_relay()
        clients = await asyncio.wait_for(asyncio.gather(first, second), 2)
        self.assertFalse(relay_task.done())
        await asyncio.gather(*(client.close() for client in clients))
        await asyncio.wait_for(relay_task, 2)

    async def test_legacy_uploader_listener_config_and_audio_unchanged(self):
        self.start_relay()
        async with websockets.connect(self.url + '?session_id=legacy&role=listen') as sink:
            async with websockets.connect(self.url + '?session_id=legacy',
                                          extra_headers={'X-Access-Token': 'OLDTOKEN'}) as source:
                config = '{"rate":48000,"channels":2,"format":"f32le"}'
                await source.send(config)
                self.assertEqual(await asyncio.wait_for(sink.recv(), 1), config)
                await source.send(b'\0\1\2\3' * 64)
                self.assertEqual(await asyncio.wait_for(sink.recv(), 1), b'\0\1\2\3' * 64)
                self.uploader_auth.assert_any_await('legacy', 'OLDTOKEN', '1', '')
                self.listener_auth.assert_any_await('legacy', '1', '')

    async def test_v2_viewer_and_producer_credentials_unchanged(self):
        self.start_relay()
        async with websockets.connect(self.url + '?session_id=v2&role=listen&protocol=2&key=viewer') as sink:
            async with websockets.connect(self.url + '?session_id=v2&protocol=2',
                                          extra_headers={'X-Ghost-Lease': 'producer'}) as source:
                await source.send(b'{"rate":16000}')
                self.assertEqual(await asyncio.wait_for(sink.recv(), 1), '{"rate":16000}')
                self.uploader_auth.assert_any_await('v2', '', '2', 'producer')
                self.listener_auth.assert_any_await('v2', '2', 'viewer')

    async def test_active_silent_socket_prevents_idle_exit_and_rechecks_auth(self):
        relay_task = self.start_relay(.05)
        async with websockets.connect(self.url + '?session_id=silent&role=listen'):
            await asyncio.sleep(.2)
            self.assertFalse(relay_task.done())
            self.assertGreaterEqual(self.listener_auth.await_count, 3)
        await asyncio.wait_for(relay_task, 1)

    async def test_pending_tcp_handshake_prevents_idle_exit(self):
        relay_task = self.start_relay(.05)
        raw_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_socket.setblocking(False)
        await asyncio.get_running_loop().sock_connect(raw_socket, self.listener.getsockname())
        await asyncio.sleep(.2)
        self.assertFalse(relay_task.done())
        async with websockets.connect(self.url + '?session_id=slow&role=listen', sock=raw_socket):
            self.assertFalse(relay_task.done())
        await asyncio.wait_for(relay_task, 1)

    async def test_pending_auth_prevents_idle_exit_then_rejection_keeps_1008(self):
        auth_result = asyncio.get_running_loop().create_future()

        async def slow_auth(*_args):
            return await auth_result

        self.listener_auth.side_effect = slow_auth
        relay_task = self.start_relay(.05)
        async with websockets.connect(self.url + '?session_id=slow-auth&role=listen') as client:
            await asyncio.sleep(.2)
            self.assertFalse(relay_task.done())
            auth_result.set_result((None, 'session expired'))
            with self.assertRaises(ConnectionClosed) as caught:
                await client.recv()
            self.assertEqual(caught.exception.code, 1008)
            self.assertEqual(caught.exception.reason, 'session expired')
        await asyncio.wait_for(relay_task, 1)

    async def test_reactivated_relay_accepts_pending_connection_on_same_socket(self):
        await asyncio.wait_for(self.start_relay(.03), 1)
        client_task = asyncio.ensure_future(websockets.connect(self.url + '?session_id=second&role=listen'))
        await asyncio.sleep(.03)
        self.assertFalse(client_task.done())
        relay_task = self.start_relay(.03)
        client = await asyncio.wait_for(client_task, 1)
        await client.close()
        await asyncio.wait_for(relay_task, 1)

    async def test_inflight_fanout_prevents_idle_exit(self):
        release_send = asyncio.get_running_loop().create_future()

        async def delayed_send(_message):
            await release_send

        room = relay.Room()
        sink = MagicMock()
        sink.send = delayed_send
        room.sinks.add(sink)
        relay.schedule_fanout(room, b'last buffered frame')
        relay_task = self.start_relay(.03)
        await asyncio.sleep(.1)
        self.assertFalse(relay_task.done())
        self.assertTrue(relay.fanout_tasks)
        release_send.set_result(None)
        await asyncio.wait_for(relay_task, 1)

    async def test_connection_accepted_at_idle_boundary_is_not_rejected(self):
        # Force a TCP accept between the idle check and listener close.
        original_close = asyncio.base_events.Server.close
        raw_sockets = []
        injected = False

        def close_with_race(server):
            nonlocal injected
            if not injected:
                injected = True
                connection, accepted = socket.socketpair()
                accepted.setblocking(False)
                asyncio.create_task(asyncio.get_running_loop().connect_accepted_socket(
                    server._protocol_factory, accepted))
                connection.setblocking(False)
                raw_sockets.append(connection)
            original_close(server)

        with patch.object(asyncio.base_events.Server, 'close', close_with_race):
            relay_task = self.start_relay(.03)
            for _attempt in range(100):
                if raw_sockets:
                    break
                if relay_task.done():
                    await relay_task
                await asyncio.sleep(.01)
            self.assertTrue(raw_sockets)
            await asyncio.sleep(.06)
            self.assertFalse(relay_task.done())
            async with websockets.connect(self.url + '?session_id=race&role=listen', sock=raw_sockets[0]):
                # The resumed listener must also accept another client now.
                async with websockets.connect(self.url + '?session_id=next&role=listen'):
                    self.assertFalse(relay_task.done())
            await asyncio.wait_for(relay_task, 1)


if __name__ == '__main__':
    unittest.main()
