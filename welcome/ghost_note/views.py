import json
import uuid

from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .models import GhostSession, GhostTextMessage


def _get_or_create_session(session_id):
    session_id = (session_id or '').strip()
    if not session_id:
        return None
    try:
        uuid.UUID(session_id)
    except ValueError:
        return None
    session, _ = GhostSession.objects.get_or_create(session_id=session_id)
    return session


@require_GET
def viewer(request, session_id):
    session = get_object_or_404(GhostSession, session_id=session_id)
    return render(request, 'ghost_note/viewer.html', {
        'session_id': session.session_id,
    })


@csrf_exempt
@require_POST
def upload_screenshot(request):
    session_id = request.GET.get('session_id') or request.headers.get('X-Session-Id', '')
    session = _get_or_create_session(session_id)
    if session is None:
        return JsonResponse({'error': 'invalid session_id'}, status=400)

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
    session = _get_or_create_session(session_id)
    if session is None:
        return JsonResponse({'error': 'invalid session_id'}, status=400)
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

    session = _get_or_create_session(session_id)
    if session is None:
        return JsonResponse({'error': 'invalid session_id'}, status=400)

    GhostTextMessage.objects.create(session=session, text=text)
    return JsonResponse({'ok': True})


@require_GET
def poll_text(request):
    session_id = request.GET.get('session_id', '')
    session = _get_or_create_session(session_id)
    if session is None:
        return JsonResponse({'error': 'invalid session_id'}, status=400)

    pending = list(
        GhostTextMessage.objects.filter(session=session, delivered=False).order_by('created_at')[:50]
    )
    messages = [m.text for m in pending]
    if pending:
        GhostTextMessage.objects.filter(pk__in=[m.pk for m in pending]).update(delivered=True)

    return JsonResponse({'messages': messages})


@csrf_exempt
@require_POST
def register_session(request):
    """Create a new session (optional — client may generate UUID locally)."""
    session = GhostSession.objects.create()
    return JsonResponse({'session_id': str(session.session_id)})
