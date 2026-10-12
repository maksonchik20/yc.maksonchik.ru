from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("webhook_tg", "0015_whoupdatebotevent")]

    operations = [
        migrations.CreateModel(
            name="WhoUpdateActivityBatch",
            fields=[
                ("batch_key", models.UUIDField(primary_key=True, editable=False, serialize=False)),
                ("source", models.CharField(max_length=64)),
                ("new_archived_messages", models.PositiveIntegerField(default=0)),
                ("first_message_pk", models.BigIntegerField(null=True, blank=True)),
                ("last_message_pk", models.BigIntegerField()),
                ("last_event_at", models.DateTimeField(null=True, blank=True, db_index=True)),
                ("received_at", models.DateTimeField(auto_now_add=True, db_index=True)),
            ],
            options={
                "verbose_name": "Сводка активности WhoUpdate",
                "verbose_name_plural": "Сводки активности WhoUpdate",
                "ordering": ("-received_at",),
            },
        ),
    ]
