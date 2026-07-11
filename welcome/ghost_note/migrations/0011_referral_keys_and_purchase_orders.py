import secrets
import string

from django.db import migrations, models
import django.db.models.deletion
import uuid


REFERRAL_KEY_ALPHABET = string.ascii_uppercase + string.digits


def _generate_referral_key(used):
    for _ in range(128):
        key = ''.join(secrets.choice(REFERRAL_KEY_ALPHABET) for _ in range(8))
        if key not in used:
            used.add(key)
            return key
    raise RuntimeError('Unable to generate referral key')


def fill_referral_keys(apps, schema_editor):
    GhostUser = apps.get_model('ghost_note', 'GhostUser')
    used = set(
        GhostUser.objects.exclude(referral_key='').values_list('referral_key', flat=True)
    )
    for user in GhostUser.objects.all().iterator():
        if user.referral_key:
            used.add(user.referral_key)
            continue
        user.referral_key = _generate_referral_key(used)
        user.save(update_fields=['referral_key'])


class Migration(migrations.Migration):

    dependencies = [
        ('ghost_note', '0010_commission_is_paid_and_payouts'),
    ]

    operations = [
        migrations.AddField(
            model_name='ghostuser',
            name='referral_key',
            field=models.CharField(
                blank=True,
                default='',
                editable=False,
                max_length=16,
                verbose_name='Реферальный ключ',
            ),
        ),
        migrations.RunPython(fill_referral_keys, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='ghostuser',
            name='referral_key',
            field=models.CharField(
                blank=True,
                default='',
                editable=False,
                max_length=16,
                unique=True,
                verbose_name='Реферальный ключ',
            ),
        ),
        migrations.CreateModel(
            name='GhostPurchaseOrder',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('public_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('customer_name', models.CharField(max_length=128, verbose_name='Имя покупателя')),
                ('referral_key_input', models.CharField(blank=True, max_length=16, verbose_name='Введённый реферальный ключ')),
                ('access_type', models.CharField(
                    choices=[('local', 'Локальный доступ'), ('remote', 'Удалённый доступ')],
                    max_length=8,
                    verbose_name='Тип доступа',
                )),
                ('duration_minutes', models.PositiveSmallIntegerField(verbose_name='Длительность (мин)')),
                ('starts_at', models.DateTimeField(verbose_name='Начало доступа')),
                ('amount', models.DecimalField(decimal_places=2, max_digits=10, verbose_name='Сумма')),
                ('yookassa_payment_id', models.CharField(
                    blank=True,
                    db_index=True,
                    default='',
                    max_length=64,
                    verbose_name='ID платежа ЮKassa',
                )),
                ('status', models.CharField(
                    choices=[
                        ('pending', 'Ожидает оплаты'),
                        ('paid', 'Оплачен'),
                        ('canceled', 'Отменён'),
                        ('failed', 'Ошибка'),
                    ],
                    default='pending',
                    max_length=16,
                    verbose_name='Статус',
                )),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('paid_at', models.DateTimeField(blank=True, null=True)),
                ('referrer', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='purchase_referrals',
                    to='ghost_note.ghostuser',
                    verbose_name='Пригласивший',
                )),
                ('token', models.OneToOneField(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='purchase_order',
                    to='ghost_note.ghostaccesstoken',
                    verbose_name='Выданный токен',
                )),
                ('user', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='purchase_orders',
                    to='ghost_note.ghostuser',
                    verbose_name='Пользователь',
                )),
            ],
            options={
                'verbose_name': 'Заказ Ghost Note',
                'verbose_name_plural': 'Заказы Ghost Note',
                'ordering': ['-created_at'],
            },
        ),
    ]
