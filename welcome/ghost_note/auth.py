import uuid
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .models import GhostAccessToken, GhostSession


GHOST_SESSION_NAMESPACE = uuid.UUID('6ba7b810-9dad-11d1-80b4-00c04fd430c8')
TOKEN_ACTIVITY_WRITE_SECONDS = 30


def format_token_datetime(dt):
    if dt is None:
        return ''
    return timezone.localtime(dt).strftime('%d.%m.%Y %H:%M') + ' МСК'


def format_expires_at(dt):
    return format_token_datetime(dt)


def record_token_activity(token, now):
    cutoff = now - timedelta(seconds=TOKEN_ACTIVITY_WRITE_SECONDS)
    if token.last_used_at is not None and token.last_used_at > cutoff:
        return False

    # Gate stale concurrent readers in SQL; an activity write must not trigger
    # the payment/referral side effects of GhostAccessToken.save().
    updated = GhostAccessToken.objects.filter(pk=token.pk).filter(
        Q(last_used_at__isnull=True) | Q(last_used_at__lte=cutoff),
    ).update(last_used_at=now)
    if updated:
        token.last_used_at = now
    return bool(updated)


def validate_access_token(token_str):
    token_str = (token_str or '').strip()
    if not token_str:
        return None, 'invalid', None

    try:
        token = GhostAccessToken.objects.get(token__iexact=token_str)
    except GhostAccessToken.DoesNotExist:
        return None, 'invalid', None

    if not token.is_active:
        return None, 'invalid', None

    now = timezone.now()
    if now < token.starts_at:
        return None, 'not_started', token.starts_at

    if now >= token.expires_at:
        return None, 'expired', token.expires_at

    record_token_activity(token, now)
    return token, 'ok', token.expires_at


def session_token_valid(session):
    # V2 credentials may never be bypassed through a legacy endpoint.
    from .models import GhostSecuritySession
    if GhostSecuritySession.objects.filter(session_id=session.pk).exists():
        return False
    if session.access_token_id is None:
        return True
    token = session.access_token
    now = timezone.now()
    return token.is_active and token.starts_at <= now < token.expires_at


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
        session_id__iexact=canonical_id,
        defaults={'session_id': canonical_id, 'access_token': access_token},
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
