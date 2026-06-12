import json
import uuid

from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .models import AiChatMessage, AiChatSession
from .yandex_client import create_response, parse_image_base64


def _parse_json_body(request):
    try:
        return dict(json.loads(request.body.decode('utf-8')))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _bad_request(message):
    return JsonResponse({'error': message}, status=400)


def _get_session(session_id):
    if not session_id:
        return AiChatSession.objects.create(session_id=str(uuid.uuid4()))

    session, _ = AiChatSession.objects.get_or_create(session_id=session_id)
    return session


@csrf_exempt
def send_message(request):
    if request.method != 'POST':
        return HttpResponse('Send POST request')

    body = _parse_json_body(request)
    if body is None:
        return _bad_request('Invalid JSON body')

    message = body.get('message')
    if message is None or not isinstance(message, str) or not message.strip():
        return _bad_request("Pass a non-empty string in the 'message' field")

    session_id = body.get('session_id')
    if session_id is not None and not isinstance(session_id, str):
        return _bad_request("Pass a string in the 'session_id' field")

    instructions = body.get('instructions', '')
    if instructions is not None and not isinstance(instructions, str):
        return _bad_request("Pass a string in the 'instructions' field")

    image_base64 = body.get('image_base64')
    if image_base64 is not None and not isinstance(image_base64, str):
        return _bad_request("Pass a string in the 'image_base64' field")

    no_reasoning = body.get('no_reasoning', False)
    if not isinstance(no_reasoning, bool):
        return _bad_request("Pass a boolean in the 'no_reasoning' field")

    try:
        image_bytes, mime_type = parse_image_base64(image_base64)
        session = _get_session(session_id)
        reply, response_id = create_response(
            prompt=message.strip(),
            previous_response_id=session.previous_response_id,
            image_bytes=image_bytes,
            mime_type=mime_type,
            instructions=instructions or '',
            reasoning_effort='none' if no_reasoning else None,
        )
    except ValueError as exc:
        return _bad_request(str(exc))
    except RuntimeError as exc:
        return JsonResponse({'error': str(exc)}, status=502)
    except Exception as exc:
        return JsonResponse({'error': str(exc)}, status=500)

    AiChatMessage.objects.create(session=session, role=AiChatMessage.ROLE_USER, text=message.strip())
    AiChatMessage.objects.create(session=session, role=AiChatMessage.ROLE_ASSISTANT, text=reply)

    session.previous_response_id = response_id
    session.save(update_fields=['previous_response_id', 'updated_at'])

    return JsonResponse({
        'session_id': session.session_id,
        'reply': reply,
        'response_id': response_id,
    })


@csrf_exempt
def clear_history(request):
    if request.method != 'POST':
        return HttpResponse('Send POST request')

    body = _parse_json_body(request)
    if body is None:
        return _bad_request('Invalid JSON body')

    session_id = body.get('session_id')
    if not session_id or not isinstance(session_id, str):
        return _bad_request("Pass a string in the 'session_id' field")

    try:
        session = AiChatSession.objects.get(session_id=session_id)
    except AiChatSession.DoesNotExist:
        return JsonResponse({'status': 'cleared', 'session_id': session_id})

    session.clear_history()
    return JsonResponse({'status': 'cleared', 'session_id': session_id})


@csrf_exempt
def get_history(request):
    if request.method != 'POST':
        return HttpResponse('Send POST request')

    body = _parse_json_body(request)
    if body is None:
        return _bad_request('Invalid JSON body')

    session_id = body.get('session_id')
    if not session_id or not isinstance(session_id, str):
        return _bad_request("Pass a string in the 'session_id' field")

    try:
        session = AiChatSession.objects.get(session_id=session_id)
    except AiChatSession.DoesNotExist:
        return JsonResponse({'session_id': session_id, 'messages': []})

    messages = [
        {'role': item.role, 'text': item.text, 'created_at': item.created_at.isoformat()}
        for item in session.messages.all()
    ]
    return JsonResponse({'session_id': session_id, 'messages': messages})
