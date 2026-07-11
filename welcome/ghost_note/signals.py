from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import GhostAccessToken
from .telegram_notify import notify_admin_test_token


@receiver(post_save, sender=GhostAccessToken)
def notify_on_test_token_created(sender, instance, created, **kwargs):
    if created and instance.token_type == GhostAccessToken.TokenType.TEST:
        notify_admin_test_token(instance)
