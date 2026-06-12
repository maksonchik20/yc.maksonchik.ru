import uuid

from django.db import models


class AiChatSession(models.Model):
    session_id = models.CharField(max_length=64, unique=True, default=uuid.uuid4, editable=False)
    previous_response_id = models.CharField(max_length=255, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'AI chat session'
        verbose_name_plural = 'AI chat sessions'

    def clear_history(self):
        self.messages.all().delete()
        self.previous_response_id = None
        self.save(update_fields=['previous_response_id', 'updated_at'])


class AiChatMessage(models.Model):
    ROLE_USER = 'user'
    ROLE_ASSISTANT = 'assistant'
    ROLE_CHOICES = (
        (ROLE_USER, 'User'),
        (ROLE_ASSISTANT, 'Assistant'),
    )

    session = models.ForeignKey(AiChatSession, on_delete=models.CASCADE, related_name='messages')
    role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        verbose_name = 'AI chat message'
        verbose_name_plural = 'AI chat messages'
