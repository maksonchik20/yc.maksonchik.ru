from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ghost_note', '0002_ghostaccesstoken_ghostsession_access_token'),
    ]

    operations = [
        migrations.AddField(
            model_name='ghostsession',
            name='audio_enabled',
            field=models.BooleanField(default=False, verbose_name='Трансляция звука'),
        ),
    ]
