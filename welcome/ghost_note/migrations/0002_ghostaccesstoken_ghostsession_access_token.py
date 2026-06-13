import ghost_note.models
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('ghost_note', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='GhostAccessToken',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('token', models.CharField(default=ghost_note.models.generate_access_token, editable=False, max_length=64, unique=True)),
                ('label', models.CharField(blank=True, max_length=128, verbose_name='Заметка')),
                ('expires_at', models.DateTimeField(verbose_name='Действителен до')),
                ('is_active', models.BooleanField(default=True, verbose_name='Активен')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('last_used_at', models.DateTimeField(blank=True, null=True, verbose_name='Последнее использование')),
            ],
            options={
                'verbose_name': 'Токен доступа Ghost Note',
                'verbose_name_plural': 'Токены доступа Ghost Note',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddField(
            model_name='ghostsession',
            name='access_token',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='sessions', to='ghost_note.ghostaccesstoken'),
        ),
    ]
