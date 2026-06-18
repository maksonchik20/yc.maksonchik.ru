from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('ghost_note', '0005_ghostaccesstoken_starts_at'),
    ]

    operations = [
        migrations.CreateModel(
            name='GhostUser',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=128, verbose_name='Имя')),
                ('telegram_username', models.CharField(
                    blank=True,
                    help_text='Без @, например: ivanov',
                    max_length=64,
                    verbose_name='Telegram username',
                )),
                ('notes', models.TextField(blank=True, verbose_name='Заметки')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создан')),
                ('referred_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='referrals',
                    to='ghost_note.ghostuser',
                    verbose_name='Пригласил',
                )),
            ],
            options={
                'verbose_name': 'Пользователь Ghost Note',
                'verbose_name_plural': 'Пользователи Ghost Note',
                'ordering': ['name'],
            },
        ),
        migrations.AddField(
            model_name='ghostaccesstoken',
            name='token_type',
            field=models.CharField(
                choices=[('test', 'Тестовый'), ('real', 'Реальный')],
                default='real',
                max_length=8,
                verbose_name='Тип токена',
            ),
        ),
        migrations.AddField(
            model_name='ghostaccesstoken',
            name='payment_amount',
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text='Только для реального токена',
                max_digits=10,
                null=True,
                verbose_name='Сумма оплаты',
            ),
        ),
        migrations.AddField(
            model_name='ghostaccesstoken',
            name='user',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='tokens',
                to='ghost_note.ghostuser',
                verbose_name='Пользователь',
            ),
        ),
        migrations.CreateModel(
            name='GhostReferralCommission',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('payment_amount', models.DecimalField(decimal_places=2, max_digits=10, verbose_name='Сумма покупки')),
                ('commission_amount', models.DecimalField(decimal_places=2, max_digits=10, verbose_name='Комиссия (20%)')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('referrer', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='commissions_earned',
                    to='ghost_note.ghostuser',
                    verbose_name='Кто привёл',
                )),
                ('referred_user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='commissions_generated',
                    to='ghost_note.ghostuser',
                    verbose_name='Кого привели',
                )),
                ('token', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='referral_commission',
                    to='ghost_note.ghostaccesstoken',
                    verbose_name='Токен покупки',
                )),
            ],
            options={
                'verbose_name': 'Реферальная комиссия',
                'verbose_name_plural': 'Реферальные комиссии',
                'ordering': ['-created_at'],
            },
        ),
    ]
