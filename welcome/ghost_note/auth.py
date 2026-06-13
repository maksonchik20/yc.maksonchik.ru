from django.utils import timezone

from .models import GhostAccessToken


def format_expires_at(dt):
    if dt is None:
        return ''
    return timezone.localtime(dt).strftime('%d.%m.%Y %H:%M')


def validate_access_token(token_str):
    token_str = (token_str or '').strip()
    if not token_str:
        return None, 'invalid', None

    try:
        token = GhostAccessToken.objects.get(token=token_str)
    except GhostAccessToken.DoesNotExist:
        return None, 'invalid', None

    if not token.is_active:
        return None, 'invalid', None

    if timezone.now() >= token.expires_at:
        return None, 'expired', token.expires_at

    token.last_used_at = timezone.now()
    token.save(update_fields=['last_used_at'])
    return token, 'ok', token.expires_at


def session_token_valid(session):
    if session.access_token_id is None:
        return True
    token = session.access_token
    return token.is_active and timezone.now() < token.expires_at


def get_token_from_request(request):
    token = request.headers.get('X-Access-Token', '')
    if not token:
        token = request.GET.get('token', '')
    if not token and request.method == 'POST':
        if request.content_type and 'json' in request.content_type:
            try:
                import json
                payload = json.loads(request.body.decode('utf-8'))
                token = payload.get('token', '')
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
    return (token or '').strip()
