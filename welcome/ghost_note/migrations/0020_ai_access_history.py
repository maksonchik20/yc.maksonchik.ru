# AI permission is opt-in for all existing and newly created tokens.

from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('ghost_note', '0019_security_v2'),
    ]

    operations = [
        migrations.CreateModel(
            name='GhostAIRequest',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('request_id', models.UUIDField()),
                ('conversation_id', models.UUIDField()),
                ('image_hash', models.CharField(max_length=64)),
                ('image', models.BinaryField()),
                ('answer', models.TextField(blank=True)),
                ('status', models.CharField(choices=[('pending', 'Выполняется'), ('succeeded', 'Готово'), ('failed', 'Ошибка')], default='pending', max_length=12)),
                ('error_code', models.CharField(blank=True, max_length=48)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.CreateModel(
            name='GhostAIResetRequest',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('request_id', models.UUIDField()),
                ('conversation_id', models.UUIDField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.CreateModel(
            name='GhostAIThread',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('device_key_hash', models.CharField(max_length=64)),
                ('conversation_id', models.UUIDField(default=uuid.uuid4, editable=False)),
                ('busy_request', models.UUIDField(blank=True, null=True)),
                ('busy_since', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.AddField(
            model_name='ghostaccesstoken',
            name='allow_ai',
            field=models.BooleanField(default=False, help_text='Включается вручную. По умолчанию выключен, в том числе для тестовых токенов.', verbose_name='Доступ с нейросетью'),
        ),
        migrations.AddField(
            model_name='ghostaithread',
            name='access_token',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='ai_threads', to='ghost_note.ghostaccesstoken'),
        ),
        migrations.AddField(
            model_name='ghostairesetrequest',
            name='thread',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='resets', to='ghost_note.ghostaithread'),
        ),
        migrations.AddField(
            model_name='ghostairequest',
            name='thread',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='requests', to='ghost_note.ghostaithread'),
        ),
        migrations.AddConstraint(
            model_name='ghostaithread',
            constraint=models.UniqueConstraint(fields=('access_token', 'device_key_hash'), name='ghost_ai_token_device_unique'),
        ),
        migrations.AddConstraint(
            model_name='ghostairesetrequest',
            constraint=models.UniqueConstraint(fields=('thread', 'request_id'), name='ghost_ai_reset_unique'),
        ),
        migrations.AddIndex(
            model_name='ghostairequest',
            index=models.Index(fields=['thread', 'conversation_id', 'created_at'], name='ghost_ai_history_idx'),
        ),
        migrations.AddConstraint(
            model_name='ghostairequest',
            constraint=models.UniqueConstraint(fields=('thread', 'request_id'), name='ghost_ai_request_unique'),
        ),
    ]
