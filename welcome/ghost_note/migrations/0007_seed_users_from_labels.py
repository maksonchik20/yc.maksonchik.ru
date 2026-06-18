from django.db import migrations


def normalize_user_name(label):
    name = (label or '').strip()
    if not name:
        return ''
    lowered = name.lower()
    suffixes = (
        ' real',
        ' test',
        ' тест',
        ' подруга',
        ' (inf)',
    )
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            if lowered.endswith(suffix):
                name = name[: -len(suffix)].strip()
                lowered = name.lower()
                changed = True
                break
    return name


def infer_token_type(label):
    lowered = (label or '').lower()
    if 'test' in lowered or 'тест' in lowered:
        return 'test'
    return 'real'


def seed_users_from_labels(apps, schema_editor):
    GhostUser = apps.get_model('ghost_note', 'GhostUser')
    GhostAccessToken = apps.get_model('ghost_note', 'GhostAccessToken')

    users_by_key = {}
    for token in GhostAccessToken.objects.all().order_by('created_at'):
        normalized = normalize_user_name(token.label)
        if not normalized:
            continue

        key = normalized.casefold()
        if key not in users_by_key:
            users_by_key[key] = GhostUser.objects.create(name=normalized)

        token.user = users_by_key[key]
        token.token_type = infer_token_type(token.label)
        token.save(update_fields=['user', 'token_type'])


def unseed_users_from_labels(apps, schema_editor):
    GhostUser = apps.get_model('ghost_note', 'GhostUser')
    GhostAccessToken = apps.get_model('ghost_note', 'GhostAccessToken')

    GhostAccessToken.objects.update(user=None, token_type='real')
    GhostUser.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('ghost_note', '0006_ghost_users_referrals'),
    ]

    operations = [
        migrations.RunPython(seed_users_from_labels, unseed_users_from_labels),
    ]
