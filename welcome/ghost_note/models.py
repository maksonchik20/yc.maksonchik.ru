import secrets
import string
import uuid

from django.db import models
from django.utils import timezone

ACCESS_TOKEN_LENGTH = 6
ACCESS_TOKEN_ALPHABET = string.ascii_uppercase + string.digits


def new_session_id():
    return str(uuid.uuid4())


def generate_access_token():
    for _ in range(64):
        token = ''.join(
            secrets.choice(ACCESS_TOKEN_ALPHABET)
            for _ in range(ACCESS_TOKEN_LENGTH)
        )
        if not GhostAccessToken.objects.filter(token=token).exists():
            return token
    raise RuntimeError('Unable to generate a unique access token')


class GhostAccessToken(models.Model):
    token = models.CharField(
        max_length=64,
        unique=True,
        default=generate_access_token,
        editable=False,
        verbose_name='Токен',
    )
    label = models.CharField(max_length=128, blank=True, verbose_name='Заметка')
    expires_at = models.DateTimeField(verbose_name='Действителен до')
    is_active = models.BooleanField(default=True, verbose_name='Активен')
    allow_local = models.BooleanField(default=True, verbose_name='Локальный доступ')
    allow_remote = models.BooleanField(default=True, verbose_name='Удалённый доступ')
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True, verbose_name='Последнее использование')

    class Meta:
        verbose_name = 'Токен доступа Ghost Note'
        verbose_name_plural = 'Токены доступа Ghost Note'
        ordering = ['-created_at']

    def __str__(self):
        label = self.label or self.token
        return f'{label} (до {self.expires_at:%d.%m.%Y %H:%M})'

    @property
    def is_valid(self):
        return self.is_active and timezone.now() < self.expires_at


class GhostSession(models.Model):
    session_id = models.CharField(max_length=36, unique=True, default=new_session_id, editable=False)
    access_token = models.ForeignKey(
        GhostAccessToken,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sessions',
    )
    screenshot = models.BinaryField(null=True, blank=True)
    screenshot_updated_at = models.DateTimeField(null=True, blank=True)
    audio_enabled = models.BooleanField(default=False, verbose_name='Трансляция звука')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Ghost Note session'
        verbose_name_plural = 'Ghost Note sessions'

    def save_screenshot(self, data):
        self.screenshot = data
        self.screenshot_updated_at = timezone.now()
        self.save(update_fields=['screenshot', 'screenshot_updated_at', 'updated_at'])


class GhostTextMessage(models.Model):
    session = models.ForeignKey(GhostSession, on_delete=models.CASCADE, related_name='text_messages')
    text = models.TextField()
    delivered = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        verbose_name = 'Ghost Note text'
        verbose_name_plural = 'Ghost Note texts'
