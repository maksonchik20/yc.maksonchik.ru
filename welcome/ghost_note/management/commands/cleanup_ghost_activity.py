from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from ghost_note.models import GhostAccessToken, GhostTokenScreenshot


class Command(BaseCommand):
    help = 'Удалить снимки, срок хранения которых закончился (в том числе неактивных токенов).'

    def handle(self, *args, **options):
        deleted = 0
        for token in GhostAccessToken.objects.filter(archived_screenshots__isnull=False).distinct().iterator():
            count, _ = GhostTokenScreenshot.objects.filter(access_token=token,
                created_at__lt=timezone.now()-timedelta(days=max(1, min(30, token.screenshot_retention_days)))).delete()
            deleted += count
        self.stdout.write('Deleted expired screenshots: {}'.format(deleted))
