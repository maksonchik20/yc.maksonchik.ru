from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('ghost_note', '0004_ghostaccesstoken_mode_access'),
    ]

    operations = [
        migrations.AddField(
            model_name='ghostaccesstoken',
            name='starts_at',
            field=models.DateTimeField(
                default=django.utils.timezone.now,
                verbose_name='Действителен с',
            ),
        ),
    ]
