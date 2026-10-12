"""Owner-only viewer links. Never call this helper from public list endpoints."""

from urllib.parse import quote

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from .models import GhostSecuritySession


def active_security_session(token):
    if not token.is_valid or not (token.allow_remote or token.allow_ai):
        return None
    return (GhostSecuritySession.objects.filter(
        access_token=token, session__access_token=token, revoked_at__isnull=True,
        lease_expires_at__gt=timezone.now(),
    ).select_related('session').order_by('-session__updated_at', '-pk').first())


def viewer_path_for_token(token):
    security = active_security_session(token)
    if security:
        return protected_viewer_path(security, token)
    if token.allow_ai and not token.allow_remote:
        return None
    return reverse('ghost_viewer_token') + '?token=' + quote(token.token, safe='')


def protected_viewer_path(security, token):
    path = reverse('ghost_v2_viewer', args=[security.session.session_id]) + '?key=' + security.viewer_secret
    return path + '&mode=ai' if token.allow_ai and (security.mode == 'ai' or not token.allow_remote) else path


def owner_session_data(token):
    origin = getattr(settings, 'GHOST_PUBLIC_BASE_URL', 'https://yc.maksonchik.ru').rstrip('/')
    security = active_security_session(token)
    legacy_path = reverse('ghost_viewer_token') + '?token=' + quote(token.token, safe='')
    viewer_path = viewer_path_for_token(token)
    fieldToInfo = {
        'viewer_url': origin + viewer_path if viewer_path else None,
        'legacy_viewer_url': origin + legacy_path,
        'session_id': security.session.session_id if security else None,
        'session_protocol': 2 if security else 1,
        'session_expires_at': security.lease_expires_at.isoformat() if security else None,
    }
    # A live lease is authorization, not proof that the laptop is online.
    fieldToInfo['session_state'] = 'lease_active' if security else ('unavailable' if not viewer_path else 'legacy')
    fieldToInfo['sessions'] = []
    now = timezone.now()
    for session in token.sessions.select_related('security_v2').order_by('-updated_at')[:20]:
        protected = getattr(session, 'security_v2', None)
        if protected:
            active = (token.is_valid and (token.allow_remote or token.allow_ai) and protected.access_token_id == token.pk
                      and protected.revoked_at is None and protected.lease_expires_at > now)
            path = protected_viewer_path(protected, token)
            fieldToInfo['sessions'].append({
                'id': session.session_id, 'protocol': 2, 'active': active,
                'viewer_url': origin + path if active else None,
                'expires_at': protected.lease_expires_at.isoformat(),
                'updated_at': session.updated_at.isoformat(),
            })
        else:
            fieldToInfo['sessions'].append({
                'id': session.session_id, 'protocol': 1, 'active': token.is_valid and token.allow_remote,
                'viewer_url': origin + reverse('ghost_viewer', args=[session.session_id]),
                'expires_at': token.expires_at.isoformat(), 'updated_at': session.updated_at.isoformat(),
            })
    return fieldToInfo
