import uuid

from django.utils import timezone

from .models import GhostAccessToken, GhostSession


GHOST_SESSION_NAMESPACE = uuid.UUID('6ba7b810-9dad-11d1-80b4-00c04fd430c8')


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


def session_id_for_access_token(token_str):
    token_str = (token_str or '').strip()
    return str(uuid.uuid5(GHOST_SESSION_NAMESPACE, f'ghost-note:{token_str}'))


def get_or_create_session_for_token(access_token):
    canonical_id = session_id_for_access_token(access_token.token)
    session, created = GhostSession.objects.get_or_create(
        session_id=canonical_id,
        defaults={'access_token': access_token},
    )
    if not created and session.access_token_id != access_token.id:
        session.access_token = access_token
        session.save(update_fields=['access_token'])
    return session


def build_viewer_url(request, token_str):
    from urllib.parse import quote

    from django.urls import reverse

    token_str = (token_str or '').strip()
    path = reverse('ghost_viewer_token') + '?token=' + quote(token_str, safe='')
    return request.build_absolute_uri(path)
