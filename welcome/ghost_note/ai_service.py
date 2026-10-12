"""Device-isolated AI conversations; secrets and model calls stay on the server."""
import base64
import hashlib
import json
import math
import uuid
from datetime import timedelta

from django.db import transaction
from django.db.models.functions import Length
from django.utils import timezone

from .models import GhostAccessToken, GhostAIRequest, GhostAIResetRequest, GhostAIThread
from .security_v2 import SecurityError, canonical_session_id, require_security

MAX_IMAGE_BYTES = 8 * 1024 * 1024
BUSY_SECONDS = 120
CONTEXT_TURNS = 20
CONTEXT_IMAGES = 3
CONTEXT_IMAGE_BYTES = 12 * 1024 * 1024
CONTEXT_TEXT_CHARS = 24000
HISTORY_TURNS = 30
HISTORY_RESPONSE_BYTES = 200 * 1024
MAX_ANSWER_CHARS = 16000
IMAGE_PROMPT = 'Прочитай задание и варианты ответа на изображении. Дай краткий ответ.'


def authorized(session_id, credential):
    security = require_security(session_id, credential, remote=False)
    if not security.access_token.allow_ai:
        raise SecurityError('ai_not_allowed', 403)
    return security


def request_uuid(value):
    return uuid.UUID(canonical_session_id(value))


def _locked_thread(session_id, credential):
    security = authorized(session_id, credential)
    # Serializes thread creation with owner access changes without locking over I/O.
    GhostAccessToken.objects.select_for_update().get(pk=security.access_token_id)
    security = authorized(session_id, credential)
    thread, _created = GhostAIThread.objects.get_or_create(
        access_token=security.access_token, device_key_hash=security.device_key_hash)
    return GhostAIThread.objects.select_for_update().get(pk=thread.pk)


def _clear_stale(thread):
    if thread.busy_request and (not thread.busy_since or
            thread.busy_since <= timezone.now() - timedelta(seconds=BUSY_SECONDS)):
        GhostAIRequest.objects.filter(thread=thread, request_id=thread.busy_request,
            status=GhostAIRequest.Status.PENDING).update(
                status=GhostAIRequest.Status.FAILED, error_code='ai_timeout', completed_at=timezone.now())
        thread.busy_request = None
        thread.busy_since = None
        thread.save(update_fields=['busy_request', 'busy_since'])


def _result(request, duplicate=False):
    return {'ok': True, 'answer': request.answer, 'request_id': str(request.request_id),
            'conversation_id': str(request.conversation_id), 'duplicate': duplicate}


def _provider_messages(previous, image):
    # Keep recent text context longer than image context, bounded independently.
    selected = []
    text_remaining = CONTEXT_TEXT_CHARS
    image_remaining = CONTEXT_IMAGE_BYTES - len(image)
    for index, item in enumerate(reversed(previous[-CONTEXT_TURNS:])):
        if text_remaining <= 0:
            break
        answer = item.answer[:text_remaining]
        text_remaining -= len(answer)
        image_bytes = None
        if index < CONTEXT_IMAGES:
            size = item.image_size if hasattr(item, 'image_size') else len(item.image)
            if size <= image_remaining:
                image_bytes = bytes(item.image)
                image_remaining -= size
        selected.append((answer, image_bytes))
    selected.reverse()
    messages = []
    for answer, image_bytes in [*selected, (None, image)]:
        content = [{'type': 'text', 'text': IMAGE_PROMPT if image_bytes is not None else
                    'Предыдущее задание с изображения (изображение вне текущего контекста).'}]
        if image_bytes is not None:
            content.append({'type': 'image_url', 'image_url': {
                'url': 'data:image/jpeg;base64,' + base64.b64encode(image_bytes).decode('ascii')}})
        messages.append({'role': 'user', 'content': content})
        if answer is not None:
            messages.append({'role': 'assistant', 'content': answer})
    return messages


def _fail(thread_id, request_id, code):
    # A late worker must not clear a newer worker's claim or release its answer.
    with transaction.atomic():
        thread = GhostAIThread.objects.select_for_update().filter(pk=thread_id).first()
        if thread is None:
            return
        GhostAIRequest.objects.filter(thread=thread, request_id=request_id,
            status=GhostAIRequest.Status.PENDING).update(
                status=GhostAIRequest.Status.FAILED, error_code=code, completed_at=timezone.now())
        if thread.busy_request == request_id:
            thread.busy_request = None
            thread.busy_since = None
            thread.save(update_fields=['busy_request', 'busy_since'])


def chat(session_id, credential, request_id, image):
    request_id = request_uuid(request_id)
    # Validate envelope before allocating history; malformed data never reaches provider.
    if not image or len(image) > MAX_IMAGE_BYTES:
        raise SecurityError('invalid_image', 413 if len(image) > MAX_IMAGE_BYTES else 400)
    if not image.startswith(b'\xff\xd8\xff') or not image.endswith(b'\xff\xd9'):
        raise SecurityError('invalid_image', 400)
    image_hash = hashlib.sha256(image).hexdigest()
    with transaction.atomic():
        thread = _locked_thread(session_id, credential)
        _clear_stale(thread)
        if GhostAIResetRequest.objects.filter(thread=thread, request_id=request_id).exists():
            raise SecurityError('ai_request_conflict', 409)
        existing = GhostAIRequest.objects.defer('image').filter(thread=thread, request_id=request_id).first()
        if existing:
            if existing.image_hash != image_hash:
                raise SecurityError('ai_request_conflict', 409)
            if existing.conversation_id != thread.conversation_id:
                raise SecurityError('ai_conversation_reset', 409)
            if existing.status == GhostAIRequest.Status.SUCCEEDED:
                return _result(existing, duplicate=True)
            if existing.status == GhostAIRequest.Status.PENDING:
                raise SecurityError('ai_busy', 409)
            raise SecurityError(existing.error_code or 'ai_failed', 502)
        token = GhostAccessToken.objects.select_for_update().get(pk=thread.access_token_id)
        now = timezone.now()
        if token.last_ai_used_at:
            remaining = math.ceil(30 - (now - token.last_ai_used_at).total_seconds())
            if remaining > 0:
                raise SecurityError('ai_cooldown', 429,
                    'Подождите {} сек, не так часто.'.format(remaining), remaining)
        if thread.busy_request:
            raise SecurityError('ai_busy', 409)
        if token.ai_daily_request_limit:
            day = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
            if GhostAIRequest.objects.filter(thread__access_token=token, created_at__gte=day).count() >= token.ai_daily_request_limit:
                raise SecurityError('ai_daily_limit', 429, 'Достигнут суточный лимит AI-запросов.')
        # Reserve under the token lock before leaving the transaction or calling Qwen.
        # Failures still consume the interval; retries of an existing request do not.
        GhostAccessToken.objects.filter(pk=token.pk).update(last_ai_used_at=now)
        previous = list(GhostAIRequest.objects.filter(thread=thread,
            conversation_id=thread.conversation_id, status=GhostAIRequest.Status.SUCCEEDED
        ).defer('image').annotate(image_size=Length('image')).order_by('-created_at', '-pk')[:CONTEXT_TURNS])
        previous.reverse()
        job = GhostAIRequest.objects.create(thread=thread, request_id=request_id,
            conversation_id=thread.conversation_id, image=image, image_hash=image_hash)
        thread.busy_request = request_id
        thread.busy_since = timezone.now()
        thread.save(update_fields=['busy_request', 'busy_since'])
    try:
        from .ai_provider import complete
        answer = complete(_provider_messages(previous, image))
        if not isinstance(answer, str) or not answer.strip() or len(answer) > MAX_ANSWER_CHARS:
            raise SecurityError('ai_invalid_response', 502)
        answer = answer.strip()
        try:
            answer.encode('utf-8')
        except UnicodeEncodeError:
            raise SecurityError('ai_invalid_response', 502)
        with transaction.atomic():
            current = _locked_thread(session_id, credential)
            if current.pk != thread.pk or current.conversation_id != job.conversation_id:
                raise SecurityError('ai_conversation_reset', 409)
            if current.busy_request != request_id or (not current.busy_since or
                    current.busy_since <= timezone.now() - timedelta(seconds=BUSY_SECONDS)):
                raise SecurityError('ai_timeout', 504)
            job.answer = answer
            job.status = GhostAIRequest.Status.SUCCEEDED
            job.completed_at = timezone.now()
            job.save(update_fields=['answer', 'status', 'completed_at'])
            current.busy_request = None
            current.busy_since = None
            current.save(update_fields=['busy_request', 'busy_since'])
        return _result(job)
    except SecurityError as error:
        _fail(thread.pk, request_id, error.code)
        raise
    except Exception as error:
        code = getattr(error, 'code', '')
        if code not in {'ai_unavailable', 'ai_timeout', 'ai_rate_limited', 'ai_provider_error'}:
            code = 'ai_unavailable'
        # Never log or expose provider responses, API keys, or screenshots.
        _fail(thread.pk, request_id, code)
        raise SecurityError(code, {'ai_timeout': 504, 'ai_rate_limited': 429}.get(code, 503))


def history(session_id, credential):
    security = authorized(session_id, credential)
    thread = GhostAIThread.objects.filter(access_token=security.access_token,
        device_key_hash=security.device_key_hash).first()
    if thread is None:
        return {'ok': True, 'conversation_id': '', 'messages': [], 'answer': '', 'busy': False}
    turns = list(GhostAIRequest.objects.filter(thread=thread,
        conversation_id=thread.conversation_id, status=GhostAIRequest.Status.SUCCEEDED
    ).defer('image').order_by('-created_at', '-pk')[:HISTORY_TURNS])
    turns.reverse()
    messages = []
    for turn in turns:
        messages.extend([
            {'role': 'user', 'content': IMAGE_PROMPT, 'has_image': True,
                'created_at': turn.created_at.isoformat(), 'request_id': str(turn.request_id)},
            {'role': 'assistant', 'content': turn.answer,
                'created_at': turn.completed_at.isoformat(), 'request_id': str(turn.request_id)},
        ])
    authorized(session_id, credential)
    fieldToHistory = {'ok': True, 'conversation_id': str(thread.conversation_id), 'messages': messages,
        'answer': turns[-1].answer if turns else '',
        'busy': bool(thread.busy_request and thread.busy_since and
            thread.busy_since > timezone.now() - timedelta(seconds=BUSY_SECONDS))}
    # The native client bounds JSON to 256 KiB. Count real JSON UTF-8 bytes,
    # including escapes and the duplicated latest answer, not just characters.
    while len(messages) > 2 and len(json.dumps(fieldToHistory, ensure_ascii=False).encode('utf-8')) > HISTORY_RESPONSE_BYTES:
        del messages[:2]
    return fieldToHistory



def reset(session_id, credential, request_id):
    request_id = request_uuid(request_id)
    with transaction.atomic():
        thread = _locked_thread(session_id, credential)
        existing = GhostAIResetRequest.objects.filter(thread=thread, request_id=request_id).first()
        if existing:
            return {'ok': True, 'conversation_id': str(existing.conversation_id), 'duplicate': True}
        if GhostAIRequest.objects.filter(thread=thread, request_id=request_id).exists():
            raise SecurityError('ai_request_conflict', 409)
        _clear_stale(thread)
        if thread.busy_request:
            raise SecurityError('ai_busy', 409)
        thread.conversation_id = uuid.uuid4()
        thread.save(update_fields=['conversation_id'])
        GhostAIResetRequest.objects.create(thread=thread, request_id=request_id,
            conversation_id=thread.conversation_id)
        return {'ok': True, 'conversation_id': str(thread.conversation_id), 'duplicate': False}
