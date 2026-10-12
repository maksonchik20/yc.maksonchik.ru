from datetime import timedelta
from uuid import UUID

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import WhoUpdateActivityBatch, WhoUpdateBotEvent


class ActivityBatchConflict(ValueError):
    pass


def record_activity_batch(fieldToPayload):
    if fieldToPayload.get("source") != "who-update-main":
        raise ValueError("Unknown activity source")
    try:
        batch_key = UUID(str(fieldToPayload.get("batch_key")))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Invalid batch key")
    count = fieldToPayload.get("new_archived_messages")
    first_pk = fieldToPayload.get("first_message_pk")
    last_pk = fieldToPayload.get("last_message_pk")
    if type(count) is not int or not 0 <= count <= 5000:
        raise ValueError("Invalid archived message count")
    if type(last_pk) is not int or not 0 <= last_pk <= 9223372036854775807:
        raise ValueError("Invalid archive cursor")
    if count:
        if type(first_pk) is not int or not 0 < first_pk <= last_pk or count > last_pk - first_pk + 1:
            raise ValueError("Invalid archive range")
    elif first_pk is not None:
        raise ValueError("Empty batch must not have a first message")
    raw_event_at = fieldToPayload.get("last_event_at")
    event_at = None
    if raw_event_at is not None:
        try:
            event_at = parse_datetime(raw_event_at) if isinstance(raw_event_at, str) else None
        except (ValueError, TypeError):
            raise ValueError("Invalid event time")
        if (
            event_at is None
            or timezone.is_naive(event_at)
            or event_at > timezone.now() + timedelta(minutes=2)
        ):
            raise ValueError("Invalid event time")
    fieldToValue = {
        "source": fieldToPayload["source"],
        "new_archived_messages": count,
        "first_message_pk": first_pk,
        "last_message_pk": last_pk,
        "last_event_at": event_at,
    }
    batch, created = WhoUpdateActivityBatch.objects.get_or_create(
        batch_key=batch_key, defaults=fieldToValue,
    )
    if not created and any(getattr(batch, field) != value for field, value in fieldToValue.items()):
        raise ActivityBatchConflict("Batch key already has a different payload")
    return batch, created


def latest_activity_at():
    legacy_at = WhoUpdateBotEvent.objects.order_by("-received_at").values_list("received_at", flat=True).first()
    batch_at = (
        WhoUpdateActivityBatch.objects.filter(last_event_at__isnull=False)
        .order_by("-last_event_at").values_list("last_event_at", flat=True).first()
    )
    return max((value for value in (legacy_at, batch_at) if value is not None), default=None)
