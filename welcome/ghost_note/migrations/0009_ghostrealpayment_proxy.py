from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('ghost_note', '0008_ghostaccesstoken_token_generate_on_save'),
    ]

    operations = [
        migrations.CreateModel(
            name='GhostRealPayment',
            fields=[],
            options={
                'verbose_name': 'Реальная оплата',
                'verbose_name_plural': 'Реальные оплаты',
                'proxy': True,
                'indexes': [],
                'constraints': [],
            },
            bases=('ghost_note.ghostaccesstoken',),
        ),
    ]
