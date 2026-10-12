from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import ai_service
from .security_v2 import SecurityError
from .views_v2 import protected_response


def credentials(request):
    return request.headers.get('X-Session-Id', ''), request.headers.get('X-Ghost-Lease', '')


@csrf_exempt
@require_POST
@protected_response
def chat(request):
    session_id, credential = credentials(request)
    ai_service.authorized(session_id, credential)
    length = request.META.get('CONTENT_LENGTH', '')
    if length and (not length.isdigit() or int(length) > ai_service.MAX_IMAGE_BYTES):
        raise SecurityError('invalid_image', 413)
    if request.content_type != 'image/jpeg':
        raise SecurityError('invalid_image', 415)
    return JsonResponse(ai_service.chat(session_id, credential,
        request.headers.get('X-Request-Id', ''), request.read(ai_service.MAX_IMAGE_BYTES + 1)), json_dumps_params={'ensure_ascii': False})


@require_GET
@protected_response
def history(request):
    return JsonResponse(ai_service.history(*credentials(request)), json_dumps_params={'ensure_ascii': False})


@csrf_exempt
@require_POST
@protected_response
def reset(request):
    if request.META.get('CONTENT_LENGTH', '0') not in ('', '0'):
        raise SecurityError('invalid_request', 400)
    return JsonResponse(ai_service.reset(*credentials(request), request.headers.get('X-Request-Id', '')))
