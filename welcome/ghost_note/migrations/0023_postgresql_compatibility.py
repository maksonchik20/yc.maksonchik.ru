from django.db import migrations

import ghost_note.fields


CASE_INSENSITIVE_KEYS = (
    ('ghost_note_ghostaccesstoken', 'token', 'ghost_access_token_ci_unique'),
    ('ghost_note_ghostsession', 'session_id', 'ghost_session_id_ci_unique'),
    ('ghost_note_ghostuser', 'referral_key', 'ghost_referral_key_ci_unique'),
)


def add_case_insensitive_keys(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    quote = schema_editor.quote_name
    for table, column, index in CASE_INSENSITIVE_KEYS:
        schema_editor.execute('CREATE UNIQUE INDEX {} ON {} (UPPER({}))'.format(
            quote(index), quote(table), quote(column)))


def remove_case_insensitive_keys(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    for _table, _column, index in CASE_INSENSITIVE_KEYS:
        schema_editor.execute('DROP INDEX {}'.format(schema_editor.quote_name(index)))


class Migration(migrations.Migration):
    dependencies = [('ghost_note', '0022_token_device_defaults')]

    operations = [
        migrations.AlterField(
            model_name='ghosttextmessage', name='text', field=ghost_note.fields.NullSafeTextField()),
        migrations.RunPython(add_case_insensitive_keys, remove_case_insensitive_keys),
    ]
