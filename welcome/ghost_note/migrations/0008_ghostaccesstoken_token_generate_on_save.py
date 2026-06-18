from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ghost_note', '0007_seed_users_from_labels'),
    ]

    operations = [
        migrations.AlterField(
            model_name='ghostaccesstoken',
            name='token',
            field=models.CharField(
                blank=True,
                default='',
                editable=False,
                max_length=64,
                unique=True,
                verbose_name='Токен',
            ),
        ),
    ]
