from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ghost_note', '0009_ghostrealpayment_proxy'),
    ]

    operations = [
        migrations.AddField(
            model_name='ghostreferralcommission',
            name='is_paid',
            field=models.BooleanField(default=False, verbose_name='Выплачено'),
        ),
        migrations.CreateModel(
            name='GhostReferralPayout',
            fields=[],
            options={
                'verbose_name': 'Выплата рефералу',
                'verbose_name_plural': 'Выплаты рефералам',
                'proxy': True,
                'indexes': [],
                'constraints': [],
            },
            bases=('ghost_note.ghostuser',),
        ),
    ]
