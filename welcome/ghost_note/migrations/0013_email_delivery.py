from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ghost_note', '0012_telegram_delivery'),
    ]

    operations = [
        migrations.AddField(
            model_name='ghostpurchaseorder',
            name='customer_email',
            field=models.EmailField(blank=True, default='', max_length=254, verbose_name='E-mail покупателя'),
        ),
        migrations.AddField(
            model_name='ghostpurchaseorder',
            name='email_notified_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Токен отправлен на e-mail'),
        ),
        migrations.AddField(
            model_name='ghostpurchaseorder',
            name='email_notify_error',
            field=models.CharField(blank=True, default='', max_length=500, verbose_name='Ошибка отправки на e-mail'),
        ),
    ]
