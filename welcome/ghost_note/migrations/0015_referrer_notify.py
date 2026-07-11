from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ghost_note', '0014_trial_access'),
    ]

    operations = [
        migrations.AddField(
            model_name='ghostpurchaseorder',
            name='referrer_notified_at',
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name='Пригласивший уведомлён в Telegram',
            ),
        ),
        migrations.AddField(
            model_name='ghostpurchaseorder',
            name='referrer_notify_error',
            field=models.CharField(
                blank=True,
                default='',
                max_length=500,
                verbose_name='Ошибка уведомления пригласившего',
            ),
        ),
    ]
