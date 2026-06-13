#!/usr/bin/env python3
"""WebSocket relay: Ghost Note PC uploader -> browser listeners per session."""

import asyncio
import logging
import os
import sys
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.environ.get(
    'GHOST_PROJECT_DIR',
    '/usr/share/django-projects/welcome',
)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'welcome.settings')

import django  # noqa: E402

django.setup()

from asgiref.sync import sync_to_async  # noqa: E402

from ghost_note.auth import session_token_valid, validate_access_token  # noqa: E402
from ghost_note.models import GhostSession  # noqa: E402

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError as exc:
    raise SystemExit('Install websockets: pip install websockets') from exc

LOG = logging.getLogger('ghost_audio_relay')
HOST = os.environ.get('GHOST_AUDIO_RELAY_HOST', '127.0.0.1')
PORT = int(os.environ.get('GHOST_AUDIO_RELAY_PORT', '8767'))


class Room:
    __slots__ = ('source', 'sinks', 'last_config')

    def __init__(self):
        self.source = None
        self.sinks = set()
        self.last_config = None


ROOMS = {}


def _get_session(session_id):
    session_id = (session_id or '').strip()
    if not session_id:
        return None
    try:
        return GhostSession.objects.get(session_id=session_id)
    except GhostSession.DoesNotExist:
        return None


def _validate_listener(session_id):
    session = _get_session(session_id)
    if session is None:
        return None, 'invalid session'
    if not session_token_valid(session):
        return None, 'session expired'
    if not session.audio_enabled:
        return None, 'audio off'
    return session, None


def _validate_uploader(session_id, token):
    session = _get_session(session_id)
    if session is None:
        return None, 'invalid session'
    if not session_token_valid(session):
        return None, 'session expired'

    token_obj, error, _expires = validate_access_token(token)
    if token_obj is None:
        return None, error or 'unauthorized'

    if session.access_token_id and session.access_token_id != token_obj.id:
        return None, 'unauthorized'

    session.refresh_from_db(fields=['audio_enabled'])
    if not session.audio_enabled:
        return None, 'audio off'

    return session, None


get_session = sync_to_async(_get_session, thread_sensitive=True)
validate_listener = sync_to_async(_validate_listener, thread_sensitive=True)
validate_uploader = sync_to_async(_validate_uploader, thread_sensitive=True)


def get_room(session_id):
    room = ROOMS.get(session_id)
    if room is None:
        room = Room()
        ROOMS[session_id] = room
    return room


async def fanout(room, message):
    dead = []
    for sink in list(room.sinks):
        try:
            await sink.send(message)
        except ConnectionClosed:
            dead.append(sink)
        except Exception:
            LOG.exception('send to listener failed')
            dead.append(sink)
    for sink in dead:
        room.sinks.discard(sink)


async def handler(websocket, path):
    parsed = urlparse(path or '/')
    query = parse_qs(parsed.query)
    session_id = (query.get('session_id') or [''])[0].strip()
    role = (query.get('role') or [''])[0].strip().lower()

    token = (websocket.request_headers.get('X-Access-Token') or '').strip()
    is_listener = role == 'listen' or not token

    if is_listener:
        _session, error = await validate_listener(session_id)
        if error:
            await websocket.close(code=1008, reason=error)
            return

        room = get_room(session_id)
        room.sinks.add(websocket)
        LOG.info('listener joined %s (sinks=%d)', session_id, len(room.sinks))
        if room.last_config:
            try:
                await websocket.send(room.last_config)
            except ConnectionClosed:
                room.sinks.discard(websocket)
                return
        try:
            await websocket.wait_closed()
        finally:
            room.sinks.discard(websocket)
            LOG.info('listener left %s (sinks=%d)', session_id, len(room.sinks))
        return

    _session, error = await validate_uploader(session_id, token)
    if error:
        await websocket.close(code=1008, reason=error)
        return

    room = get_room(session_id)
    if room.source is not None:
        await websocket.close(code=1008, reason='uploader busy')
        return

    room.source = websocket
    LOG.info('uploader joined %s', session_id)
    try:
        async for message in websocket:
            if isinstance(message, str):
                room.last_config = message
            await fanout(room, message)
    except ConnectionClosed:
        pass
    finally:
        if room.source is websocket:
            room.source = None
        LOG.info('uploader left %s', session_id)


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
    )
    LOG.info('starting on %s:%s', HOST, PORT)
    async with websockets.serve(handler, HOST, PORT, max_size=2 ** 20):
        await asyncio.Future()


if __name__ == '__main__':
    asyncio.run(main())
