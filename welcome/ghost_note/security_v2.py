"""Versioned device proof and signed, short-lived GhostNote licenses.

No client-supplied version flag upgrades a token or authenticates a request.
The private P-256 signing key is supplied out of band and never returned by API.
"""

import base64
import hashlib
import json
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .auth import record_token_activity
from .models import GhostAccessToken, GhostLeaseChallenge, GhostSecuritySession, GhostSession, GhostTokenDevice

LEASE_SECONDS = 600
# An offline license may survive a short outage, but must not reserve an online
# slot for ten minutes after a crash. Producers renew every 60s (or heartbeat).
SESSION_IDLE_SECONDS = 120
HEARTBEAT_WRITE_SECONDS = 15
CHALLENGE_SECONDS = 90
MAX_AUTH_BODY = 8192
_HEX_32 = re.compile(r'^[0-9a-f]{64}$')
_HEX_SIGNATURE = re.compile(r'^[0-9a-f]{128}$')
_KEY_ID = re.compile(r'^[A-Za-z0-9_-]{1,64}$')


class SecurityError(Exception):
    def __init__(self, code='security_error', status=401, message='', retry_after=None):
        self.code = code
        self.status = status
        self.message = message
        self.retry_after = retry_after
        super().__init__(code)


def crypto_modules():
    # A missing optional v2 dependency must never stop importing the v1 URLconf.
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, utils
    except ImportError:
        raise SecurityError('security_unavailable', 503)
    return InvalidSignature, hashes, serialization, ec, utils


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def read_json(request, limit=MAX_AUTH_BODY):
    length = request.META.get('CONTENT_LENGTH', '')
    if length and (not length.isdigit() or int(length) > limit):
        raise SecurityError('invalid_request', 400)
    if len(request.body) > limit:
        raise SecurityError('invalid_request', 400)
    try:
        fieldToValue = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise SecurityError('invalid_request', 400)
    if not isinstance(fieldToValue, dict):
        raise SecurityError('invalid_request', 400)
    return fieldToValue


def token_text(fieldToValue):
    token = fieldToValue.get('token', '')
    if not isinstance(token, str) or not token or len(token) > 64 or token != token.strip():
        raise SecurityError('invalid')
    return token


def validate_token_state(token, now=None, remote=False):
    now = now or timezone.now()
    if token is None or not token.is_active:
        raise SecurityError('invalid')
    if now < token.starts_at:
        raise SecurityError('not_started')
    if now >= token.expires_at:
        raise SecurityError('expired')
    if remote and not token.allow_remote:
        raise SecurityError('remote_not_allowed', 403)


def device_key(fieldToValue):
    _invalid_signature, _hashes, _serialization, ec, _utils = crypto_modules()
    value = fieldToValue.get('device_public_key', '')
    if not isinstance(value, str) or not re.fullmatch(r'04[0-9a-f]{128}', value):
        raise SecurityError('invalid_request', 400)
    encoded = bytes.fromhex(value)
    try:
        public_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), encoded)
    except ValueError:
        raise SecurityError('invalid_request', 400)
    return public_key, sha256_hex(encoded)


def hex32(fieldToValue, name):
    value = fieldToValue.get(name, '')
    if not isinstance(value, str) or not _HEX_32.fullmatch(value):
        raise SecurityError('invalid_request', 400)
    return value


def canonical_session_id(value, allow_empty=False):
    if allow_empty and value == '':
        return ''
    if not isinstance(value, str):
        raise SecurityError('invalid_request', 400)
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise SecurityError('invalid_request', 400)
    if canonical != value:
        raise SecurityError('invalid_request', 400)
    return canonical


def load_signing_key():
    _invalid_signature, _hashes, serialization, ec, _utils = crypto_modules()
    filename = getattr(settings, 'GHOST_V2_SIGNING_KEY_FILE', None) or os.environ.get('GHOST_V2_SIGNING_KEY_FILE', '')
    key_id = getattr(settings, 'GHOST_V2_KEY_ID', None) or os.environ.get('GHOST_V2_KEY_ID', '')
    if not filename or not key_id:
        config_file = (getattr(settings, 'GHOST_V2_CONFIG_FILE', None)
                       or os.environ.get('GHOST_V2_CONFIG_FILE', '/etc/ghost-note/security-v2.json'))
        try:
            with open(config_file, 'r', encoding='utf-8') as source:
                encoded_config = source.read(4097)
            if len(encoded_config) > 4096:
                raise ValueError('config too large')
            fieldToConfig = json.loads(encoded_config)
            if not isinstance(fieldToConfig, dict):
                raise ValueError('config must be object')
            filename = filename or fieldToConfig.get('signing_key_file', '')
            key_id = key_id or fieldToConfig.get('key_id', '')
        except (OSError, ValueError, TypeError, UnicodeDecodeError):
            raise SecurityError('security_unavailable', 503)
    if not isinstance(filename, str) or not filename or not isinstance(key_id, str) or not _KEY_ID.fullmatch(key_id):
        raise SecurityError('security_unavailable', 503)
    try:
        with open(filename, 'rb') as source:
            encoded = source.read(16385)
        if len(encoded) > 16384:
            raise ValueError('key too large')
        private_key = serialization.load_pem_private_key(encoded, password=None)
        if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(private_key.curve, ec.SECP256R1):
            raise ValueError('P-256 required')
    except (OSError, ValueError, TypeError):
        raise SecurityError('security_unavailable', 503)
    return key_id, private_key


def challenge_response(fieldToValue):
    # Fail explicitly when v2 is not provisioned; never fall back to unsigned v1.
    load_signing_key()
    token_value = token_text(fieldToValue)
    _public_key, device_hash = device_key(fieldToValue)
    try:
        token = GhostAccessToken.objects.get(token__iexact=token_value)
    except GhostAccessToken.DoesNotExist:
        raise SecurityError('invalid')
    now = timezone.now()
    validate_token_state(token, now)
    challenge = secrets.token_hex(32)
    GhostLeaseChallenge.objects.create(
        challenge_hash=sha256_hex(challenge.encode('ascii')),
        access_token=token, device_key_hash=device_hash,
        expires_at=now + timedelta(seconds=CHALLENGE_SECONDS),
    )
    # Bound storage growth without a scheduler; a missing, expired proof fails identically.
    expired_ids = list(GhostLeaseChallenge.objects.filter(
        expires_at__lt=now - timedelta(minutes=10),
    ).order_by('expires_at').values_list('pk', flat=True)[:100])
    if expired_ids:
        GhostLeaseChallenge.objects.filter(pk__in=expired_ids).delete()
    return {'challenge': challenge, 'server_time': int(now.timestamp())}


def request_payload(challenge, nonce, token_value, device_hash, session_id, license_version=2):
    return ('GHOST-LEASE-REQUEST-V{}\n{}\n{}\n{}\n{}\n{}\n'.format(
        license_version, challenge, nonce, sha256_hex(token_value.encode('utf-8')), device_hash, session_id,
    )).encode('utf-8')


def raw_signature(private_key, payload):
    _invalid_signature, hashes, _serialization, ec, utils = crypto_modules()
    r, s = utils.decode_dss_signature(private_key.sign(payload, ec.ECDSA(hashes.SHA256())))
    return (r.to_bytes(32, 'big') + s.to_bytes(32, 'big')).hex()


def verify_device_signature(public_key, signature, payload):
    InvalidSignature, hashes, _serialization, ec, utils = crypto_modules()
    if not isinstance(signature, str) or not _HEX_SIGNATURE.fullmatch(signature):
        raise SecurityError('security_error')
    raw = bytes.fromhex(signature)
    encoded = utils.encode_dss_signature(int.from_bytes(raw[:32], 'big'), int.from_bytes(raw[32:], 'big'))
    try:
        public_key.verify(encoded, payload, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError):
        raise SecurityError('security_error')


def lease_response(fieldToValue):
    license_version = fieldToValue.get('license_version', 2)
    if type(license_version) is not int or license_version not in (2, 3):
        raise SecurityError('invalid_request', 400)
    key_id, signing_key = load_signing_key()
    token_value = token_text(fieldToValue)
    public_key, device_hash = device_key(fieldToValue)
    challenge = hex32(fieldToValue, 'challenge')
    nonce = hex32(fieldToValue, 'nonce')
    session_id = canonical_session_id(fieldToValue.get('session_id', ''), allow_empty=True)
    verify_device_signature(public_key, fieldToValue.get('signature'),
                            request_payload(challenge, nonce, token_value, device_hash, session_id, license_version))
    with transaction.atomic():
        try:
            token = GhostAccessToken.objects.select_for_update().get(token__iexact=token_value)
        except GhostAccessToken.DoesNotExist:
            raise SecurityError('invalid')
        now = timezone.now()
        validate_token_state(token, now)
        try:
            proof = GhostLeaseChallenge.objects.select_for_update().get(
                challenge_hash=sha256_hex(challenge.encode('ascii')),
                access_token=token, device_key_hash=device_hash,
            )
        except GhostLeaseChallenge.DoesNotExist:
            raise SecurityError('security_error')
        if proof.consumed_at is not None or now >= proof.expires_at:
            raise SecurityError('security_error')
        device = GhostTokenDevice.objects.filter(access_token=token, device_key_hash=device_hash).first()
        if device and device.blocked:
            raise SecurityError('device_blocked', 403)
        if device is None:
            if token.max_devices and token.devices.filter(blocked=False).count() >= token.max_devices:
                raise SecurityError('device_limit', 403)
            device = GhostTokenDevice.objects.create(access_token=token, device_key_hash=device_hash)
        GhostTokenDevice.objects.filter(pk=device.pk).update(last_seen_at=now)
        issued_at = int(now.timestamp())
        expires_at = min(issued_at + LEASE_SECONDS, int(token.expires_at.timestamp()))
        if expires_at <= issued_at:
            raise SecurityError('expired')
        lease_expiry = datetime.fromtimestamp(expires_at, tz=datetime_timezone.utc)
        security = None
        if session_id:
            try:
                security = GhostSecuritySession.objects.select_for_update().select_related('session').get(
                    session__session_id=session_id, access_token=token, device_key_hash=device_hash,
                )
            except GhostSecuritySession.DoesNotExist:
                raise SecurityError('security_error')
            if security.revoked_at is not None or security.session.access_token_id != token.id:
                raise SecurityError('security_error')

        # Creation AND renewal share the same token lock and capacity check.
        # Exclude the renewing session: it can recover from an outage only if
        # another device has not already reclaimed its slot and revoked it.
        siblings = token.security_sessions.filter(revoked_at__isnull=True)
        if security is not None:
            siblings = siblings.exclude(pk=security.pk)
        cutoff = now - timedelta(seconds=SESSION_IDLE_SECONDS)
        stale = (Q(lease_expires_at__lte=now) | Q(last_seen_at__lte=cutoff) |
                 Q(last_seen_at__isnull=True, created_at__lte=cutoff))
        siblings.filter(stale).update(revoked_at=now, recording_consent_at=None)
        if token.max_concurrent_sessions:
            if security is None:
                # A same-device restart replaces its prior lease. A failed
                # admission rolls this back together with device registration.
                siblings.filter(device_key_hash=device_hash).update(
                    revoked_at=now, recording_consent_at=None)
            if siblings.filter(lease_expires_at__gt=now).count() >= token.max_concurrent_sessions:
                raise SecurityError('session_limit', 403)

        if security is not None:
            security.lease_expires_at = lease_expiry
            security.last_seen_at = now
            security.save(update_fields=['lease_expires_at', 'last_seen_at'])
        else:
            session = GhostSession.objects.create(access_token=token)
            security = GhostSecuritySession.objects.create(
                session=session, access_token=token, device_key_hash=device_hash,
                producer_secret=secrets.token_hex(32), viewer_secret=secrets.token_hex(32),
                lease_expires_at=lease_expiry,
            )
            session_id = str(session.session_id)
        payload = ('\n'.join([
            'GHOST-LICENSE-V{}'.format(license_version), key_id, nonce, device_hash, sha256_hex(token_value.encode('utf-8')),
            session_id, str(issued_at), str(expires_at), str(int(token.starts_at.timestamp())),
            str(int(token.expires_at.timestamp())), '1' if token.allow_local else '0',
            '1' if token.allow_remote else '0',
        ] + (['1' if token.allow_ai else '0'] if license_version == 3 else []) + [
            security.producer_secret, security.viewer_secret, 'END',
        ]) + '\n').encode('ascii')
        signature = raw_signature(signing_key, payload)
        proof.consumed_at = now
        proof.save(update_fields=['consumed_at'])
        record_token_activity(token, now)
    return {'payload': base64.b64encode(payload).decode('ascii'), 'signature': signature}


def session_is_v2(session):
    return GhostSecuritySession.objects.filter(session_id=session.pk).exists()


def require_security(session_id, credential, viewer=False, remote=True):
    session_id = canonical_session_id(session_id)
    if not isinstance(credential, str) or not _HEX_32.fullmatch(credential):
        raise SecurityError('invalid')
    try:
        security = GhostSecuritySession.objects.select_related('session', 'access_token').defer(
            'session__screenshot').get(session__session_id=session_id)
    except GhostSecuritySession.DoesNotExist:
        raise SecurityError('invalid')
    expected = security.viewer_secret if viewer else security.producer_secret
    if not secrets.compare_digest(expected, credential) or security.revoked_at is not None:
        raise SecurityError('invalid')
    now = timezone.now()
    if now >= security.lease_expires_at:
        raise SecurityError('expired')
    if security.access_token_id is None or security.session.access_token_id != security.access_token_id:
        raise SecurityError('invalid')
    validate_token_state(security.access_token, now, remote=remote)
    if GhostTokenDevice.objects.filter(access_token=security.access_token,
            device_key_hash=security.device_key_hash, blocked=True).exists():
        raise SecurityError('device_blocked', 403)
    # Viewer traffic must never keep a crashed producer's slot alive.
    if not viewer and (security.last_seen_at is None or
            security.last_seen_at <= now - timedelta(seconds=HEARTBEAT_WRITE_SECONDS)):
        if not GhostSecuritySession.objects.filter(pk=security.pk, revoked_at__isnull=True,
                lease_expires_at__gt=now).update(last_seen_at=now):
            raise SecurityError('invalid')
        security.last_seen_at = now
    return security


def close_security_session(session_id, credential):
    """Release exactly this session, even after lease/token expiry; safe to retry."""
    session_id = canonical_session_id(session_id)
    if not isinstance(credential, str) or not _HEX_32.fullmatch(credential):
        raise SecurityError('invalid')
    try:
        security = GhostSecuritySession.objects.get(session__session_id=session_id)
    except GhostSecuritySession.DoesNotExist:
        raise SecurityError('invalid')
    if not secrets.compare_digest(security.producer_secret, credential):
        raise SecurityError('invalid')
    with transaction.atomic():
        # Match lease_response's lock order. A concurrent renewal cannot undo
        # this revocation, nor can closing an old session affect a new one.
        if security.access_token_id is not None:
            GhostAccessToken.objects.select_for_update().filter(pk=security.access_token_id).first()
        security = GhostSecuritySession.objects.select_for_update().get(pk=security.pk)
        if not secrets.compare_digest(security.producer_secret, credential):
            raise SecurityError('invalid')
        if security.revoked_at is None:
            security.revoked_at = timezone.now()
            security.recording_consent_at = None
            security.save(update_fields=['revoked_at', 'recording_consent_at'])


def require_session(session_id, credential, viewer=False):
    return require_security(session_id, credential, viewer=viewer).session


def require_text_session(session_id, credential, viewer=False):
    # AI includes human text; audio still uses the remote-only guard above.
    security = require_security(session_id, credential, viewer=viewer, remote=False)
    if not (security.access_token.allow_remote or security.access_token.allow_ai):
        raise SecurityError('text_not_allowed', 403)
    return security.session


def require_screen_session(session_id, credential, viewer=False):
    security = require_security(session_id, credential, viewer=viewer, remote=False)
    if not (security.access_token.allow_remote or security.access_token.allow_ai):
        raise SecurityError('screen_not_allowed', 403)
    return security.session
