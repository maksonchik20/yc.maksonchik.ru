import threading
from collections import deque

_lock = threading.Lock()
_buffers = {}
_MAX_BUFFER_BYTES = 2 * 1024 * 1024


def _get_buffer(session_id):
    buf = _buffers.get(session_id)
    if buf is None:
        buf = {
            'config': {'rate': 48000, 'channels': 2, 'format': 'f32le'},
            'chunks': deque(),
            'bytes': 0,
        }
        _buffers[session_id] = buf
    return buf


def append_audio(session_id, data, rate=None, channels=None):
    if not data:
        return
    with _lock:
        buf = _get_buffer(session_id)
        if rate:
            buf['config']['rate'] = int(rate)
        if channels:
            buf['config']['channels'] = int(channels)
        buf['chunks'].append(data)
        buf['bytes'] += len(data)
        while buf['bytes'] > _MAX_BUFFER_BYTES and buf['chunks']:
            dropped = buf['chunks'].popleft()
            buf['bytes'] -= len(dropped)


def poll_audio(session_id):
    with _lock:
        buf = _buffers.get(session_id)
        if not buf or not buf['chunks']:
            return None
        data = b''.join(buf['chunks'])
        buf['chunks'].clear()
        buf['bytes'] = 0
        return buf['config'].copy(), data


def clear_audio(session_id):
    with _lock:
        _buffers.pop(session_id, None)
