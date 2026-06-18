from datetime import datetime, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from ghost_note.models import GhostAccessToken


def msk(year, month, day, hour, minute, second=0):
    return timezone.make_aware(datetime(year, month, day, hour, minute, second))


MAIN_SLOT = {
    'starts_at': msk(2026, 6, 14, 14, 30),
    'expires_at': msk(2026, 6, 15, 2, 0),
}

# Основная группа: 14.06 14:30 -> 15.06 02:00 (после последних продлений).
MAIN_GROUP_TOKENS = {
    'WE1P8G', 'ELX07I', 'ROMA9T', '06DZS4', 'CRP9FF', 'IYV719', 'J7WVUU',
    '2UKCQ2', 'M3LKM8', '38E9JA', '6G4IL8', '8T4KHG', 'FE6FH3', 'A6ZPOF',
    'D96D2H', '1IH7FA', 'C0CQQP', 'B3UZN6', 'KVOGC9',
}

# Точные даты из истории продлений / создания.
EXACT_SCHEDULES = {
    'RNGOU6': (msk(2026, 6, 14, 14, 18), msk(2026, 6, 14, 16, 0)),
    'R93HML': (msk(2026, 6, 14, 17, 18), msk(2026, 6, 14, 23, 30)),
    'SH7SO4': (msk(2026, 6, 14, 13, 11), msk(2026, 6, 14, 21, 15)),
    'dBH72f08oTpBR43R__v7tin7oj_L6sHh': (msk(2026, 6, 14, 0, 21), msk(2026, 6, 14, 21, 22)),
    'F91OH8': (msk(2026, 6, 14, 16, 40), msk(2026, 6, 15, 2, 0)),
    'J0I18L': (msk(2026, 6, 14, 16, 58), msk(2026, 6, 15, 2, 0)),
    'KRYJW1': (msk(2026, 6, 15, 0, 34), msk(2026, 6, 15, 0, 35)),
    'RAKEJ2': (msk(2026, 6, 19, 2, 7), msk(2026, 6, 19, 3, 37)),
}


class Command(BaseCommand):
    help = 'Restore Ghost access token schedules from known pre-reset values.'

    def handle(self, *args, **options):
        updated = 0
        for token in GhostAccessToken.objects.all().order_by('label'):
            if token.token in EXACT_SCHEDULES:
                starts_at, expires_at = EXACT_SCHEDULES[token.token]
            elif token.token in MAIN_GROUP_TOKENS:
                starts_at = MAIN_SLOT['starts_at']
                expires_at = MAIN_SLOT['expires_at']
            else:
                starts_at = token.created_at
                expires_at = token.created_at + timedelta(days=7)

            GhostAccessToken.objects.filter(pk=token.pk).update(
                starts_at=starts_at,
                expires_at=expires_at,
            )
            updated += 1
            self.stdout.write(
                f'{token.token} | {token.label} | '
                f'{timezone.localtime(starts_at):%d.%m.%Y %H:%M} -> '
                f'{timezone.localtime(expires_at):%d.%m.%Y %H:%M}'
            )

        self.stdout.write(self.style.SUCCESS(f'Restored schedules for {updated} tokens.'))
