from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ghost_note', '0011_referral_keys_and_purchase_orders'),
    ]

    operations = [
        migrations.CreateModel(
            name='GhostTelegramContact',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('telegram_user_id', models.BigIntegerField(unique=True, verbose_name='Telegram chat_id')),
                ('username', models.CharField(blank=True, db_index=True, default='', max_length=64, verbose_name='Username')),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Telegram-контакт Ghost Note',
                'verbose_name_plural': 'Telegram-контакты Ghost Note',
            },
        ),
        migrations.AddField(
            model_name='ghostpurchaseorder',
            name='customer_telegram',
            field=models.CharField(blank=True, default='', max_length=64, verbose_name='Telegram покупателя'),
        ),
        migrations.AddField(
            model_name='ghostpurchaseorder',
            name='telegram_notified_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Токен отправлен в Telegram'),
        ),
        migrations.AddField(
            model_name='ghostpurchaseorder',
            name='telegram_notify_error',
            field=models.CharField(blank=True, default='', max_length=500, verbose_name='Ошибка отправки в Telegram'),
        ),
    ]
