from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ghost_note', '0015_referrer_notify'),
    ]

    operations = [
        migrations.CreateModel(
            name='GhostTelegramBotMessage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('telegram_user_id', models.BigIntegerField(db_index=True, verbose_name='Telegram chat_id')),
                ('username', models.CharField(blank=True, db_index=True, default='', max_length=64, verbose_name='Username')),
                ('first_name', models.CharField(blank=True, default='', max_length=128, verbose_name='Имя')),
                ('direction', models.CharField(choices=[('in', 'Входящее'), ('out', 'Исходящее')], max_length=3, verbose_name='Направление')),
                ('message_kind', models.CharField(choices=[('text', 'Текст'), ('callback', 'Callback'), ('document', 'Документ'), ('other', 'Другое')], default='text', max_length=16, verbose_name='Тип')),
                ('text', models.TextField(blank=True, default='', verbose_name='Текст')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='Когда')),
            ],
            options={
                'verbose_name': 'Сообщение Ghost Note бота',
                'verbose_name_plural': 'Сообщения Ghost Note бота',
                'ordering': ['created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='ghosttelegrambotmessage',
            index=models.Index(fields=['username', 'created_at'], name='ghost_note_botmsg_user_dt'),
        ),
        migrations.AddIndex(
            model_name='ghosttelegrambotmessage',
            index=models.Index(fields=['telegram_user_id', 'created_at'], name='ghost_note_botmsg_chat_dt'),
        ),
    ]
