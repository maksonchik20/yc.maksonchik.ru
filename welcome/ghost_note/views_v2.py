"""V2 HTTP surface. Legacy response shapes and viewer stay in views.py."""

from functools import wraps
from hashlib import sha256

from django.db.models.functions import Length
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils.http import parse_etags
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .audio_store import append_audio, clear_audio, poll_audio as consume_audio
from .models import GhostAIRequest, GhostSession, GhostTextMessage
from .security_v2 import (SecurityError, challenge_response, lease_response,
                          read_json, require_session, require_text_session, require_screen_session)


def protected_response(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            response = view(request, *args, **kwargs)
        except SecurityError as error:
            fieldToError = {'ok': False, 'error': error.code}
            if error.message:
                fieldToError['message'] = error.message
            if error.retry_after is not None:
                # Strings keep older AI JSON readers compatible (they reject numbers).
                fieldToError['retry_after'] = str(error.retry_after)
            response = JsonResponse(fieldToError, status=error.status, json_dumps_params={'ensure_ascii': False})
            if error.retry_after is not None:
                response['Retry-After'] = str(error.retry_after)
        response['Cache-Control'] = 'no-store'
        response['Referrer-Policy'] = 'no-referrer'
        response['X-Content-Type-Options'] = 'nosniff'
        return response
    return wrapped


def request_session(request, viewer=False, fieldToValue=None, session_id=None, text=False, screen=False):
    if session_id is None:
        session_id = request.GET.get('session_id') or request.headers.get('X-Session-Id', '')
        if not session_id and fieldToValue is not None:
            session_id = fieldToValue.get('session_id', '')
    header = 'X-Ghost-Viewer' if viewer else 'X-Ghost-Lease'
    credential = request.headers.get(header, '')
    if viewer and not credential:
        credential = request.GET.get('key', '')
    authorize = require_screen_session if screen else require_text_session if text else require_session
    return authorize(session_id, credential, viewer=viewer)


@csrf_exempt
@require_POST
@protected_response
def challenge(request):
    return JsonResponse(challenge_response(read_json(request)))


@csrf_exempt
@require_POST
@protected_response
def lease(request):
    return JsonResponse(lease_response(read_json(request)))


@require_GET
@protected_response
def viewer(request, session_id):
    session = request_session(request, viewer=True, session_id=str(session_id), text=True)
    viewer_key = request.headers.get('X-Ghost-Viewer', '') or request.GET.get('key', '')
    mode = request.GET.get('mode')
    ai_viewer = session.access_token.allow_ai and (mode in ('ai', 'text') or not session.access_token.allow_remote)
    # Earlier AI clients emitted mode=text; keep these existing links usable.
    text_only = mode == 'text' and not ai_viewer
    template = 'ghost_note/viewer_text_v2.html' if text_only else 'ghost_note/viewer_v2.html'
    response = render(request, template, {
        'session_id': session.session_id, 'viewer_key': viewer_key,
        'audio_allowed': session.access_token.allow_remote and not ai_viewer,
        'ai_viewer': ai_viewer,
    })
    response['X-Frame-Options'] = 'DENY'
    return response


@csrf_exempt
@require_POST
@protected_response
def upload_screenshot(request):
    session = request_session(request, screen=True)
    if len(request.body) > 10 * 1024 * 1024:
        return JsonResponse({'error': 'file too large'}, status=413)
    if not request.body:
        return JsonResponse({'error': 'empty body'}, status=400)
    session.save_screenshot(request.body)
    return JsonResponse({'ok': True, 'session_id': session.session_id})


def _screen_etag(source, image_id, captured_at, image_size):
    if captured_at is None:
        # Legacy images without a version timestamp must always be loaded afresh.
        return None
    version = '{}:{}:{}:{}'.format(source, image_id, captured_at.isoformat(), image_size)
    return '"{}"'.format(sha256(version.encode('utf-8')).hexdigest())


def _screen_response(request, queryset, *, image_field, time_field, source):
    # Read only metadata first: unchanged polls never transfer the image BLOB.
    metadata = queryset.annotate(screen_size=Length(image_field)).values_list(
        'pk', time_field, 'screen_size').first()
    if metadata is None or not metadata[2]:
        return None
    image_id, captured_at, image_size = metadata
    etag = _screen_etag(source, image_id, captured_at, image_size)
    client_etags = parse_etags(request.headers.get('If-None-Match', ''))
    if etag and any(value in ('*', etag, 'W/' + etag) for value in client_etags):
        response = HttpResponse(status=304)
    else:
        # Reload image and timestamp together in case a producer changed them
        # after the metadata query. Never label a fresh image with an old ETag.
        current = queryset.filter(pk=image_id).values_list(image_field, time_field).first()
        if current is None or not current[0]:
            return None
        image, captured_at = current
        etag = _screen_etag(source, image_id, captured_at, len(image))
        response = HttpResponse(bytes(image), content_type='image/jpeg')
    response['X-Ghost-Screen-Source'] = source
    if captured_at:
        response['X-Ghost-Screen-Time'] = captured_at.isoformat()
    if etag:
        response['ETag'] = etag
    return response


@require_GET
@protected_response
def get_screenshot(request):
    # Authorize every poll, including conditional requests, before returning 304.
    session = request_session(request, viewer=True, screen=True)
    response = _screen_response(request, GhostSession.objects.filter(pk=session.pk),
        image_field='screenshot', time_field='screenshot_updated_at', source='live')
    if response is not None:
        return response
    if session.access_token.allow_ai:
        security = session.security_v2
        # Old AI executables only send an image when asking Qwen. Show that
        # image without ever mixing devices or exposing previous sessions.
        latest = GhostAIRequest.objects.filter(thread__access_token=session.access_token,
            thread__device_key_hash=security.device_key_hash, created_at__gte=security.created_at
        ).order_by('-created_at', '-pk')
        response = _screen_response(request, latest,
            image_field='image', time_field='created_at', source='ai-request')
        if response is not None:
            return response
    return HttpResponse(status=204)


@csrf_exempt
@require_POST
@protected_response
def post_text(request):
    fieldToValue = read_json(request, limit=65536)
    session = request_session(request, viewer=True, fieldToValue=fieldToValue, text=True)
    text = fieldToValue.get('text', '')
    if not isinstance(text, str):
        return JsonResponse({'error': 'text must be string'}, status=400)
    GhostTextMessage.objects.create(session=session, text=text)
    return JsonResponse({'ok': True})


@require_GET
@protected_response
def poll_text(request):
    session = request_session(request, text=True)
    pending = list(GhostTextMessage.objects.filter(session=session, delivered=False).order_by('created_at', 'pk')[:50])
    messages = [message.text for message in pending]
    if pending:
        GhostTextMessage.objects.filter(pk__in=[message.pk for message in pending]).update(delivered=True)
    return JsonResponse({'messages': messages}, json_dumps_params={'ensure_ascii': False})


def set_audio(request, enabled):
    fieldToValue = read_json(request)
    session = request_session(request, viewer=True, fieldToValue=fieldToValue)
    session.audio_enabled = enabled
    session.save(update_fields=['audio_enabled', 'updated_at'])
    if not enabled:
        clear_audio(str(session.session_id))
    return JsonResponse({'ok': True})


@csrf_exempt
@require_POST
@protected_response
def audio_on(request):
    return set_audio(request, True)


@csrf_exempt
@require_POST
@protected_response
def audio_off(request):
    return set_audio(request, False)


@require_GET
@protected_response
def audio_status(request):
    session = request_session(request)
    return JsonResponse({'enabled': session.audio_enabled})


@csrf_exempt
@require_POST
@protected_response
def upload_audio(request):
    session = request_session(request)
    if not session.audio_enabled:
        return HttpResponse(status=204)
    if not request.body:
        return JsonResponse({'error': 'empty body'}, status=400)
    if len(request.body) > 512 * 1024:
        return JsonResponse({'error': 'chunk too large'}, status=413)
    rate = request.headers.get('X-Audio-Rate', '')
    channels = request.headers.get('X-Audio-Channels', '')
    append_audio(str(session.session_id), request.body,
                 rate=int(rate) if rate.isdigit() and len(rate) <= 6 else None,
                 channels=int(channels) if channels.isdigit() and len(channels) <= 2 else None)
    return JsonResponse({'ok': True})


@require_GET
@protected_response
def poll_audio(request):
    session = request_session(request, viewer=True)
    if not session.audio_enabled:
        return HttpResponse(status=204)
    result = consume_audio(str(session.session_id))
    if result is None:
        return HttpResponse(status=204)
    fieldToConfig, data = result
    response = HttpResponse(data, content_type='application/octet-stream')
    response['X-Audio-Rate'] = str(fieldToConfig.get('rate', 48000))
    response['X-Audio-Channels'] = str(fieldToConfig.get('channels', 2))
    return response
