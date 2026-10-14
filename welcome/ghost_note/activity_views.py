"""Opt-in activity archive; never implicitly enable capture on legacy clients."""
from datetime import timedelta
from io import BytesIO
import math

from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .models import GhostAccessToken, GhostSecuritySession, GhostTextMessage, GhostTokenDevice, GhostTokenScreenshot
from .security_v2 import SecurityError, canonical_session_id, close_security_session, read_json, require_security
from .views_v2 import protected_response

MAX_ARCHIVE_BYTES = 4 * 1024 * 1024
SCREENSHOT_INTERVAL = 300


def authorized(request):
    return require_security(request.headers.get('X-Session-Id', ''),
                            request.headers.get('X-Ghost-Lease', ''), remote=False)


def lock_session(request):
    security = authorized(request)
    GhostAccessToken.objects.select_for_update().get(pk=security.access_token_id)
    security = authorized(request)
    # The token is already locked above. PostgreSQL cannot lock the nullable
    # side of the outer join produced by select_related('access_token').
    return GhostSecuritySession.objects.select_for_update().get(pk=security.pk)


def require_consent(security):
    if not security.access_token.activity_recording_enabled or security.recording_consent_at is None:
        raise SecurityError('recording_not_allowed', 403)


@require_GET
@protected_response
def state(request):
    security = authorized(request)
    now = timezone.now()
    GhostSecuritySession.objects.filter(pk=security.pk).update(last_seen_at=now)
    GhostTokenDevice.objects.filter(access_token=security.access_token,
        device_key_hash=security.device_key_hash).update(last_seen_at=now)
    return JsonResponse({'ok': True, 'recording_allowed': security.access_token.activity_recording_enabled,
        'recording_active': bool(security.access_token.activity_recording_enabled and security.recording_consent_at),
        'interval_seconds': str(SCREENSHOT_INTERVAL),
        'retention_days': str(max(1, min(30, security.access_token.screenshot_retention_days)))})


@csrf_exempt
@require_POST
@protected_response
def consent(request):
    fieldToValue = read_json(request, limit=2048)
    enabled = fieldToValue.get('enabled')
    mode = fieldToValue.get('mode', '')
    label = fieldToValue.get('device_name', '')
    if type(enabled) is not bool or mode not in ('local', 'remote', 'ai') or not isinstance(label, str) or len(label) > 100:
        raise SecurityError('invalid_request', 400)
    with transaction.atomic():
        security = lock_session(request)
        if enabled and not security.access_token.activity_recording_enabled:
            raise SecurityError('recording_not_allowed', 403)
        security.recording_consent_at = timezone.now() if enabled else None
        security.mode = mode
        security.last_seen_at = timezone.now()
        security.save(update_fields=['recording_consent_at', 'mode', 'last_seen_at'])
        if label:
            GhostTokenDevice.objects.filter(access_token=security.access_token,
                device_key_hash=security.device_key_hash).update(label=label)
    return JsonResponse({'ok': True, 'recording_active': enabled})


def normalize_jpeg(raw):
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(BytesIO(raw)) as image:
            if image.format != 'JPEG' or image.width * image.height > 20_000_000:
                raise ValueError()
            image.thumbnail((1920, 1080))
            output = BytesIO()
            image.convert('RGB').save(output, format='JPEG', quality=70)
            return output.getvalue()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        raise SecurityError('invalid_image', 400) from None


@csrf_exempt
@require_POST
@protected_response
def screenshot(request):
    security = authorized(request)
    require_consent(security)
    length = request.META.get('CONTENT_LENGTH', '')
    if length and (not length.isdigit() or int(length) > MAX_ARCHIVE_BYTES):
        raise SecurityError('invalid_image', 413)
    if request.content_type != 'image/jpeg':
        raise SecurityError('invalid_image', 415)
    raw = request.read(MAX_ARCHIVE_BYTES + 1)
    if not raw or len(raw) > MAX_ARCHIVE_BYTES:
        raise SecurityError('invalid_image', 413)
    # Reject repeated uploads before decoding; check again under the row lock.
    def interval_check(value, now):
        latest = GhostTokenScreenshot.objects.filter(access_token=value.access_token,
            session__device_key_hash=value.device_key_hash).order_by('-created_at').values_list('created_at', flat=True).first()
        if latest:
            wait = math.ceil(SCREENSHOT_INTERVAL - (now - latest).total_seconds())
            if wait > 0:
                raise SecurityError('archive_cooldown', 429, retry_after=wait)
    interval_check(security, timezone.now())
    image = normalize_jpeg(raw)
    with transaction.atomic():
        security = lock_session(request)
        require_consent(security)
        now = timezone.now()
        interval_check(security, now)
        GhostTokenScreenshot.objects.create(access_token=security.access_token, session=security, image=image)
        security.last_archive_at = now
        security.last_seen_at = now
        security.save(update_fields=['last_archive_at', 'last_seen_at'])
        cutoff = now - timedelta(days=max(1, min(30, security.access_token.screenshot_retention_days)))
        GhostTokenScreenshot.objects.filter(access_token=security.access_token, created_at__lt=cutoff).delete()
    return JsonResponse({'ok': True})


@csrf_exempt
@require_POST
@protected_response
def text(request):
    fieldToValue = read_json(request, limit=65536)
    message = fieldToValue.get('text', '')
    event_id = canonical_session_id(fieldToValue.get('event_id', ''))
    if not isinstance(message, str) or not message or len(message) > 16000:
        raise SecurityError('invalid_request', 400)
    with transaction.atomic():
        security = lock_session(request)
        require_consent(security)
        # This is a local archive only; never put it in the remote delivery queue.
        existing = GhostTextMessage.objects.filter(session=security.session, event_id=event_id).first()
        if existing and existing.text != message:
            raise SecurityError('event_conflict', 409)
        if existing is None:
            if GhostTextMessage.objects.filter(session__access_token=security.access_token, source='local',
                    created_at__gte=timezone.now()-timedelta(minutes=1)).count() >= 60:
                raise SecurityError('activity_rate_limited', 429, retry_after=60)
            GhostTextMessage.objects.create(session=security.session, text=message, event_id=event_id,
                                           source='local', delivered=True)
    return JsonResponse({'ok': True})


@csrf_exempt
@require_POST
@protected_response
def close_session(request):
    close_security_session(request.headers.get('X-Session-Id', ''),
                           request.headers.get('X-Ghost-Lease', ''))
    return JsonResponse({'ok': True})
