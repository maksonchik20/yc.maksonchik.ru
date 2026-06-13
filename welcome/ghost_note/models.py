import uuid

from django.db import models
from django.utils import timezone


def new_session_id():
    return str(uuid.uuid4())


class GhostSession(models.Model):
    session_id = models.CharField(max_length=36, unique=True, default=new_session_id, editable=False)
    screenshot = models.BinaryField(null=True, blank=True)
    screenshot_updated_at = models.DateTimeField(null=True, blank=True)
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
