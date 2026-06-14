from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ghost_note', '0003_ghostsession_audio_enabled'),
    ]

    operations = [
        migrations.AddField(
            model_name='ghostaccesstoken',
            name='allow_local',
            field=models.BooleanField(default=True, verbose_name='Локальный доступ'),
        ),
        migrations.AddField(
            model_name='ghostaccesstoken',
            name='allow_remote',
            field=models.BooleanField(default=True, verbose_name='Удалённый доступ'),
        ),
    ]
