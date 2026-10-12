from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("ghost_note", "0017_telegram_outbox")]
    operations = [
        migrations.AddField(
            model_name="ghostpurchaseorder",
            name="checkout_at",
            field=models.DateTimeField(blank=True, null=True, db_index=True),
        ),
        migrations.CreateModel(
            name="GhostPanelTokenRequest",
            fields=[
                ("request_id", models.UUIDField(primary_key=True, serialize=False)),
                ("data_hash", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "token",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="ghost_note.ghostaccesstoken",
                    ),
                ),
            ],
        ),
    ]
