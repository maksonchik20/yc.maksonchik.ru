import json
import uuid

from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .auth import (
    format_expires_at,
    get_token_from_request,
    session_token_valid,
    validate_access_token,
)
from .models import GhostSession, GhostTextMessage

JSON_UTF8 = {'ensure_ascii': False}


def _auth_error_response(error, expires_at=None, status=401):
    payload = {'ok': False, 'error': error}
    if expires_at is not None:
        payload['expires_at'] = format_expires_at(expires_at)
    return JsonResponse(payload, status=status)


def _get_session(session_id):
    session_id = (session_id or '').strip()
    if not session_id:
        return None
    try:
        uuid.UUID(session_id)
    except ValueError:
        return None
    try:
        return GhostSession.objects.get(session_id=session_id)
    except GhostSession.DoesNotExist:
        return None


def _get_or_create_session(session_id, access_token=None):
    session_id = (session_id or '').strip()
    if not session_id:
        return None
    try:
        uuid.UUID(session_id)
    except ValueError:
        return None

    session, created = GhostSession.objects.get_or_create(session_id=session_id)
    if created and access_token is not None:
        session.access_token = access_token
        session.save(update_fields=['access_token'])
    elif access_token is not None and session.access_token_id is None:
        session.access_token = access_token
        session.save(update_fields=['access_token'])
    return session


def _require_valid_token(request):
    token_str = get_token_from_request(request)
    token, error, expires_at = validate_access_token(token_str)
    if token is None:
        return None, _auth_error_response(error, expires_at)
    return token, None


def _require_session_access(session, token=None):
    if not session_token_valid(session):
        expires_at = session.access_token.expires_at if session.access_token_id else None
        return _auth_error_response('expired', expires_at)
    if token is not None and session.access_token_id and session.access_token_id != token.id:
        return _auth_error_response('invalid')
    return None


@csrf_exempt
@require_POST
def validate_token(request):
    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'ok': False, 'error': 'invalid'}, status=400)

    token_str = payload.get('token', '')
    token, error, expires_at = validate_access_token(token_str)
    if token is None:
        response = {'ok': False, 'error': error}
        if expires_at is not None:
            response['expires_at'] = format_expires_at(expires_at)
        return JsonResponse(response, status=401)

    return JsonResponse({
        'ok': True,
        'expires_at': format_expires_at(expires_at),
    })


@require_GET
def viewer(request, session_id):
    session = get_object_or_404(GhostSession, session_id=session_id)
    denied = _require_session_access(session)
    if denied is not None:
        return denied
    return render(request, 'ghost_note/viewer.html', {
        'session_id': session.session_id,
    })


@csrf_exempt
@require_POST
def upload_screenshot(request):
    token, denied = _require_valid_token(request)
    if denied is not None:
        return denied

    session_id = request.GET.get('session_id') or request.headers.get('X-Session-Id', '')
    session = _get_or_create_session(session_id, access_token=token)
    if session is None:
        return JsonResponse({'error': 'invalid session_id'}, status=400)

    denied = _require_session_access(session, token=token)
    if denied is not None:
        return denied

    data = request.body
    if not data:
        return JsonResponse({'error': 'empty body'}, status=400)
    if len(data) > 10 * 1024 * 1024:
        return JsonResponse({'error': 'file too large'}, status=413)

    session.save_screenshot(data)
    return JsonResponse({'ok': True, 'session_id': session.session_id})


@require_GET
def get_screenshot(request):
    session_id = request.GET.get('session_id', '')
    session = _get_session(session_id)
    if session is None:
        return JsonResponse({'error': 'invalid session_id'}, status=404)

    denied = _require_session_access(session)
    if denied is not None:
        return denied

    if not session.screenshot:
        return HttpResponse(status=204)

    resp = HttpResponse(session.screenshot, content_type='image/jpeg')
    if session.screenshot_updated_at:
        resp['Last-Modified'] = session.screenshot_updated_at.strftime('%a, %d %b %Y %H:%M:%S GMT')
    resp['Cache-Control'] = 'no-cache'
    return resp


@csrf_exempt
@require_POST
def post_text(request):
    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'error': 'invalid json'}, status=400)

    session_id = payload.get('session_id', '')
    text = payload.get('text', '')
    if not isinstance(text, str):
        return JsonResponse({'error': 'text must be string'}, status=400)

    session = _get_session(session_id)
    if session is None:
        return JsonResponse({'error': 'invalid session_id'}, status=404)

    denied = _require_session_access(session)
    if denied is not None:
        return denied

    GhostTextMessage.objects.create(session=session, text=text)
    return JsonResponse({'ok': True})


@require_GET
def poll_text(request):
    token, denied = _require_valid_token(request)
    if denied is not None:
        return denied

    session_id = request.GET.get('session_id', '')
    session = _get_session(session_id)
    if session is None:
        return JsonResponse({'error': 'invalid session_id'}, status=404)

    denied = _require_session_access(session, token=token)
    if denied is not None:
        return denied

    pending = list(
        GhostTextMessage.objects.filter(session=session, delivered=False).order_by('created_at')[:50]
    )
    messages = [m.text for m in pending]
    if pending:
        GhostTextMessage.objects.filter(pk__in=[m.pk for m in pending]).update(delivered=True)

    return JsonResponse({'messages': messages}, json_dumps_params=JSON_UTF8)


@csrf_exempt
@require_POST
def register_session(request):
    token, denied = _require_valid_token(request)
    if denied is not None:
        return denied

    session = GhostSession.objects.create(access_token=token)
    return JsonResponse({'session_id': str(session.session_id)})
