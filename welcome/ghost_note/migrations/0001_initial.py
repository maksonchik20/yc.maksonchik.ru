import uuid

from django.db import migrations, models


def new_session_id():
    return str(uuid.uuid4())


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='GhostSession',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('session_id', models.CharField(default=new_session_id, editable=False, max_length=36, unique=True)),
                ('screenshot', models.BinaryField(blank=True, null=True)),
                ('screenshot_updated_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Ghost Note session',
                'verbose_name_plural': 'Ghost Note sessions',
            },
        ),
        migrations.CreateModel(
            name='GhostTextMessage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('text', models.TextField()),
                ('delivered', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('session', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='text_messages', to='ghost_note.ghostsession')),
            ],
            options={
                'verbose_name': 'Ghost Note text',
                'verbose_name_plural': 'Ghost Note texts',
                'ordering': ['created_at'],
            },
        ),
    ]
