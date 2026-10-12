from django.db import migrations, models


def set_database_defaults(schema_editor, devices, sessions):
    if schema_editor.connection.vendor not in ('mysql', 'postgresql'):
        return
    quote = schema_editor.quote_name
    for column, value in [('max_devices', devices), ('max_concurrent_sessions', sessions)]:
        schema_editor.execute('ALTER TABLE {} ALTER COLUMN {} SET DEFAULT {}'.format(
            quote('ghost_note_ghostaccesstoken'), quote(column), value))


def forwards(apps, schema_editor):
    # Set defaults for future INSERTs; never update limits on existing tokens.
    set_database_defaults(schema_editor, 2, 1)


def backwards(apps, schema_editor):
    # Restore the compatibility defaults introduced by migration 0021.
    set_database_defaults(schema_editor, 0, 0)


class Migration(migrations.Migration):
    dependencies = [('ghost_note', '0021_activity_security')]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AlterField(
                model_name='ghostaccesstoken',
                name='max_devices',
                field=models.PositiveSmallIntegerField(default=2, verbose_name='Максимум устройств',
                    help_text='0 — без ограничений. Применяется к защищённым версиям; старые .exe продолжают работать.'),
            )],
            database_operations=[migrations.RunPython(forwards, backwards, atomic=False)],
        ),
    ]
