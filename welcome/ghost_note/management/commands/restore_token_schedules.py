import re
from datetime import datetime

from django.contrib.admin.models import LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand
from django.utils import timezone

from ghost_note.models import GhostAccessToken


def msk(year, month, day, hour, minute, second=0):
    return timezone.make_aware(datetime(year, month, day, hour, minute, second))


EXPIRES_RE = re.compile(
    r'до (\d{2})\.(\d{2})\.(\d{4}) (\d{2}):(\d{2})'
)

# Последние известные даты до массового сброса 19.06.2026 ~02:01.
# Источник: django_admin_log + подтверждённые значения из переписки.
EXPIRES_BY_ID = {
    1: msk(2026, 6, 14, 21, 22),   # API подтверждал 21:22
    2: msk(2026, 6, 15, 5, 0),     # admin: 05:00 МСК
    3: msk(2026, 6, 14, 23, 59),
    4: msk(2026, 6, 15, 12, 0),
    5: msk(2026, 6, 15, 16, 57),
    6: msk(2026, 6, 15, 15, 30),
    7: msk(2026, 6, 15, 2, 0),     # bulk-продление 17 токенов
    8: msk(2026, 6, 15, 2, 0),
    9: msk(2026, 6, 15, 12, 0),
    10: msk(2026, 6, 15, 14, 30),
    11: msk(2026, 6, 17, 14, 30),
    12: msk(2026, 6, 18, 12, 0),
    13: msk(2026, 6, 15, 2, 0),
    14: msk(2026, 6, 15, 2, 0),
    15: msk(2026, 6, 15, 2, 0),
    16: msk(2026, 6, 15, 2, 0),
    17: msk(2026, 6, 15, 12, 0),
    18: msk(2026, 6, 15, 2, 0),
    19: msk(2026, 6, 15, 2, 0),
    20: msk(2026, 6, 15, 2, 0),
    22: msk(2026, 6, 15, 12, 0),
    23: msk(2026, 6, 16, 18, 30),
    24: msk(2026, 6, 15, 15, 0),
    25: msk(2026, 6, 14, 23, 30),
    26: msk(2026, 6, 15, 16, 0),
    27: msk(2026, 6, 15, 12, 0),
    28: msk(2026, 6, 15, 15, 0),
    29: msk(2026, 6, 15, 15, 0),
    30: msk(2026, 6, 15, 2, 34),
    31: msk(2026, 6, 15, 16, 0),
    32: msk(2026, 6, 15, 23, 59),
    33: msk(2026, 6, 15, 15, 40),
    34: msk(2026, 6, 15, 13, 0),
    35: msk(2026, 6, 16, 12, 30),
    36: msk(2026, 6, 17, 13, 0),
    37: msk(2026, 6, 17, 13, 0),
    38: msk(2026, 6, 17, 18, 57),
    39: msk(2026, 6, 19, 12, 0),
    40: msk(2026, 6, 19, 18, 30),
    41: msk(2026, 6, 19, 18, 30),
}

STARTS_BY_ID = {
    1: msk(2026, 6, 14, 0, 21),
    2: msk(2026, 6, 14, 13, 11),
    7: msk(2026, 6, 14, 14, 30),
    8: msk(2026, 6, 14, 14, 30),
    9: msk(2026, 6, 14, 14, 30),
    10: msk(2026, 6, 14, 14, 30),
    11: msk(2026, 6, 14, 14, 30),
    12: msk(2026, 6, 14, 14, 30),
    13: msk(2026, 6, 14, 14, 30),
    14: msk(2026, 6, 14, 14, 30),
    15: msk(2026, 6, 14, 14, 30),
    16: msk(2026, 6, 14, 14, 30),
    17: msk(2026, 6, 14, 14, 30),
    18: msk(2026, 6, 14, 14, 30),
    19: msk(2026, 6, 14, 14, 30),
    20: msk(2026, 6, 14, 14, 30),
    22: msk(2026, 6, 14, 16, 38),
    23: msk(2026, 6, 14, 16, 40),
    24: msk(2026, 6, 14, 16, 58),
    25: msk(2026, 6, 14, 17, 18),
    26: msk(2026, 6, 14, 14, 35),
    27: msk(2026, 6, 14, 20, 22),
    28: msk(2026, 6, 14, 21, 12),
    29: msk(2026, 6, 14, 21, 14),
    30: msk(2026, 6, 15, 0, 34),
    31: msk(2026, 6, 15, 0, 49),
    32: msk(2026, 6, 15, 1, 19),
    33: msk(2026, 6, 15, 6, 38),
    34: msk(2026, 6, 15, 8, 49),
    35: msk(2026, 6, 15, 20, 51),
    36: msk(2026, 6, 16, 16, 47),
    37: msk(2026, 6, 16, 18, 46),
    38: msk(2026, 6, 17, 13, 57),
    39: msk(2026, 6, 17, 14, 16),
    40: msk(2026, 6, 17, 19, 4),
    41: msk(2026, 6, 19, 15, 0),
}

MAIN_GROUP_START = msk(2026, 6, 14, 14, 30)
WIPE_CUTOFF = msk(2026, 6, 19, 2, 1)


def parse_expires_from_repr(object_repr):
    match = EXPIRES_RE.search(object_repr or '')
    if not match:
        return None
    day, month, year, hour, minute = map(int, match.groups())
    return msk(year, month, day, hour, minute)


def expires_from_admin_log(token_id):
    ct = ContentType.objects.get_for_model(GhostAccessToken)
    entries = (
        LogEntry.objects.filter(
            content_type=ct,
            object_id=str(token_id),
            action_time__lt=WIPE_CUTOFF,
        )
        .order_by('action_time')
    )
    last = entries.last()
    if not last:
        return None
    return parse_expires_from_repr(last.object_repr)


class Command(BaseCommand):
    help = 'Restore Ghost access token schedules from django_admin_log and known values.'

    def handle(self, *args, **options):
        updated = 0
        for token in GhostAccessToken.objects.all().order_by('id'):
            expires_at = EXPIRES_BY_ID.get(token.id)
            if expires_at is None:
                expires_at = expires_from_admin_log(token.id)

            starts_at = STARTS_BY_ID.get(token.id)
            if starts_at is None and token.id in EXPIRES_BY_ID:
                if EXPIRES_BY_ID[token.id] == msk(2026, 6, 15, 2, 0):
                    starts_at = MAIN_GROUP_START
            if starts_at is None:
                starts_at = token.created_at

            if expires_at is None:
                self.stderr.write(
                    self.style.WARNING(
                        f'Skip id={token.id} {token.token} ({token.label}): no expiry data'
                    )
                )
                continue

            GhostAccessToken.objects.filter(pk=token.pk).update(
                starts_at=starts_at,
                expires_at=expires_at,
            )
            updated += 1
            self.stdout.write(
                f'id={token.id:2} {token.token:8} {token.label[:22]:22} | '
                f'{timezone.localtime(starts_at):%d.%m.%Y %H:%M} -> '
                f'{timezone.localtime(expires_at):%d.%m.%Y %H:%M}'
            )

        self.stdout.write(self.style.SUCCESS(f'Restored schedules for {updated} tokens.'))
