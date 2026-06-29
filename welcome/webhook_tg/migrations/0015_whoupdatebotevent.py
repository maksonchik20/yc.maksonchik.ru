from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("webhook_tg", "0014_alter_message_text"),
    ]

    operations = [
        migrations.CreateModel(
            name="WhoUpdateBotEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("chat_id", models.BigIntegerField(blank=True, null=True, verbose_name="Chat id")),
                ("message_id", models.BigIntegerField(blank=True, null=True, verbose_name="Message id")),
                (
                    "business_connection_id",
                    models.CharField(blank=True, default="", max_length=255, verbose_name="Business connection id"),
                ),
                ("username_from", models.CharField(blank=True, default="", max_length=255, verbose_name="Username")),
                ("first_name", models.CharField(blank=True, default="", max_length=255, verbose_name="First name")),
                ("payload", models.TextField(blank=True, default="", verbose_name="Payload")),
                ("received_at", models.DateTimeField(auto_now_add=True, verbose_name="Получено")),
            ],
            options={
                "verbose_name": "Событие WhoUpdateBot",
                "verbose_name_plural": "События WhoUpdateBot",
            },
        ),
        migrations.AddIndex(
            model_name="whoupdatebotevent",
            index=models.Index(fields=["-received_at"], name="webhook_tg__receive_6a0f0d_idx"),
        ),
    ]
