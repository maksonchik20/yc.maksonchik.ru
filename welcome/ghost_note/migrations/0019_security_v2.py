from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('ghost_note', '0018_owner_panel')]
    operations = [
        migrations.CreateModel(
            name='GhostLeaseChallenge',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('challenge_hash', models.CharField(max_length=64, unique=True)),
                ('device_key_hash', models.CharField(max_length=64)),
                ('expires_at', models.DateTimeField(db_index=True)),
                ('consumed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('access_token', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='ghost_note.ghostaccesstoken')),
            ],
        ),
        migrations.CreateModel(
            name='GhostSecuritySession',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('device_key_hash', models.CharField(max_length=64)),
                ('producer_secret', models.CharField(max_length=64)),
                ('viewer_secret', models.CharField(max_length=64)),
                ('lease_expires_at', models.DateTimeField(db_index=True)),
                ('revoked_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('access_token', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='security_sessions', to='ghost_note.ghostaccesstoken')),
                ('session', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='security_v2', to='ghost_note.ghostsession')),
            ],
        ),
    ]
