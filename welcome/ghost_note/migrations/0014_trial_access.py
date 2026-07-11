from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ghost_note', '0013_email_delivery'),
    ]

    operations = [
        migrations.AddField(
            model_name='ghosttelegramcontact',
            name='last_trial_at',
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name='Последний пробный доступ',
            ),
        ),
    ]
