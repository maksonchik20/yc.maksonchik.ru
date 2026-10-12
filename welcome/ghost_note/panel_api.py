import hashlib
import hmac
import json
import uuid
from pathlib import Path
from datetime import datetime
from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from . import panel_data
from .models import (
    GhostUser,
    GhostPanelTokenRequest,
    GhostReferralCommission,
    GhostAccessToken,
)


def secret():
    value = getattr(settings, "OWNER_PANEL_BRIDGE_TOKEN", None)
    if value is not None:
        return value
    try:
        return json.loads(Path("/srv/maksonchik/owner-panel.json").read_text()).get(
            "bridge_token", ""
        )
    except (OSError, ValueError):
        return ""


def create_token(data):
    from .admin import GhostAccessTokenAdminForm

    if not isinstance(data, dict):
        raise ValueError("JSON object required")
    key = uuid.UUID(data.get("request_id", ""))
    # Omitted and explicit false are equivalent, including retries created before AI existed.
    fieldToHashValue = dict(data)
    if fieldToHashValue.get("allow_ai") is False:
        fieldToHashValue.pop("allow_ai")
    digest = hashlib.sha256(
        json.dumps(fieldToHashValue, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    with transaction.atomic():
        record, created = GhostPanelTokenRequest.objects.get_or_create(
            request_id=key, defaults={"data_hash": digest}
        )
        if not created:
            if record.data_hash != digest:
                return {
                    "error": "Ключ запроса уже использован для другого токена."
                }, 409
            t = record.token
            return {
                "id": t.pk,
                "token": t.token,
                "user": panel_data.label(t.user),
                "expires_at": timezone.localtime(t.expires_at).isoformat(),
            }, 200
        user = GhostUser.objects.get(pk=int(data["user_id"]))
        fieldToValue = {
            "user": user.pk,
            "token_type": data.get("token_type", "real"),
            "payment_amount": data.get("amount", ""),
            "is_active": "on",
        }
        for flag in ("allow_local", "allow_remote", "allow_ai"):
            value = data.get(flag, False) if flag == "allow_ai" else data.get(flag)
            if not isinstance(value, bool):
                raise ValueError("Выберите режим доступа.")
            fieldToValue[flag] = "on" if value else ""
        for name in ("starts_at", "expires_at"):
            value = data.get(name)
            if value:
                at = datetime.fromisoformat(value)
                if timezone.is_aware(at):
                    at = timezone.localtime(at)
                fieldToValue[name + "_0"] = at.strftime("%Y-%m-%d")
                fieldToValue[name + "_1"] = at.strftime("%H:%M:%S")
        for name in ('max_devices', 'max_concurrent_sessions', 'screenshot_retention_days', 'ai_daily_request_limit'):
            fieldToValue[name] = GhostAccessToken._meta.get_field(name).default
        fieldToValue['activity_recording_enabled'] = 'on'
        form = GhostAccessTokenAdminForm(data=fieldToValue)
        if not form.is_valid():
            transaction.set_rollback(True)
            return {
                "error": "Проверьте поля токена.",
                "fields": form.errors.get_json_data(),
            }, 400
        token = form.save()
        record.token = token
        record.save(update_fields=["token"])
        return {
            "id": token.pk,
            "token": token.token,
            "user": panel_data.label(user),
            "expires_at": timezone.localtime(token.expires_at).isoformat(),
        }, 201


def update_token(data):
    from .admin import GhostAccessTokenAdminForm

    editable = (
        "label",
        "starts_at",
        "expires_at",
        "is_active",
        "allow_local",
        "allow_remote",
        "allow_ai",
    )
    if (
        not isinstance(data, dict)
        or set(data) not in ({"id", "revision", *editable}, {"id", "revision", *editable} - {"allow_ai"})
        or type(data.get("id")) is not int
        or not 0 < data["id"] <= 9223372036854775807
        or not isinstance(data.get("revision"), str)
        or len(data["revision"]) != 64
        or any(
            type(data.get(flag)) is not bool
            for flag in ("is_active", "allow_local", "allow_remote")
        )
        or ("allow_ai" in data and type(data["allow_ai"]) is not bool)
        or not isinstance(data.get("label"), str)
        or len(data["label"]) > 128
    ):
        raise ValueError("Invalid token update")
    with transaction.atomic():
        token = (
            GhostAccessToken.objects.select_for_update().filter(pk=data["id"]).first()
        )
        if token is None:
            return {"error": "Токен не найден. Обновите список."}, 404
        if panel_data.token_revision(token) != data["revision"]:
            return {
                "error": "Токен уже изменился. Загрузите актуальные данные перед редактированием."
            }, 409
        fieldToValue = {
            "user": token.user_id or "",
            "token_type": token.token_type,
            "payment_amount": (
                token.payment_amount if token.payment_amount is not None else ""
            ),
            "label": data["label"],
        }
        for name in ("starts_at", "expires_at"):
            if not isinstance(data[name], str) or len(data[name]) > 40:
                raise ValueError("Invalid date")
            value = datetime.fromisoformat(data[name])
            if not timezone.is_aware(value):
                raise ValueError("Timezone required")
            local = timezone.localtime(value)
            fieldToValue[name + "_0"] = local.strftime("%Y-%m-%d")
            fieldToValue[name + "_1"] = local.strftime("%H:%M:%S.%f")
        for name in ("is_active", "allow_local", "allow_remote"):
            fieldToValue[name] = "on" if data[name] else ""
        fieldToValue["allow_ai"] = "on" if data.get("allow_ai", token.allow_ai) else ""
        for name in ('max_devices', 'max_concurrent_sessions', 'screenshot_retention_days', 'ai_daily_request_limit'):
            fieldToValue[name] = getattr(token, name)
        fieldToValue['activity_recording_enabled'] = 'on' if token.activity_recording_enabled else ''
        form = GhostAccessTokenAdminForm(data=fieldToValue, instance=token)
        # Preserve sub-second boundaries on ordinary note/access changes.
        for name in ("starts_at", "expires_at"):
            form.fields[name].fields[1].input_formats = [
                "%H:%M:%S.%f",
                "%H:%M:%S",
                "%H:%M",
            ]
        if not form.is_valid():
            return {
                "error": "Проверьте параметры токена.",
                "fields": form.errors.get_json_data(),
            }, 400
        changes = {name: form.cleaned_data[name] for name in editable}
        # Token.save() resynchronizes financial commissions. Operational edits
        # deliberately update only these columns and leave the ledger untouched.
        GhostAccessToken.objects.filter(pk=token.pk).update(**changes)
        token.refresh_from_db()
        return panel_data.token_detail(token), 200


def set_payout(data):
    if (
        not isinstance(data, dict)
        or type(data.get("id")) is not int
        or not 0 < data["id"] <= 9223372036854775807
        or type(data.get("is_paid")) is not bool
        or not isinstance(data.get("revision"), str)
        or len(data["revision"]) != 64
    ):
        raise ValueError("Invalid payout")
    with transaction.atomic():
        commission = (
            GhostReferralCommission.objects.select_for_update()
            .filter(pk=data["id"])
            .first()
        )
        if commission is None:
            return {"error": "Начисление больше не существует. Обновите список."}, 404
        if panel_data.payout_revision(commission) != data["revision"]:
            return {
                "error": "Начисление уже изменилось. Обновите список и проверьте сумму и статус."
            }, 409
        # Set the requested state, never invert it on retries. Touch no token or payment.
        commission.is_paid = data["is_paid"]
        commission.save(update_fields=["is_paid"])
        return {
            "id": commission.pk,
            "is_paid": commission.is_paid,
            "data": panel_data.referrals(),
        }, 200


@never_cache
@csrf_exempt
@require_http_methods(["GET", "POST"])
def bridge(request, action):
    expected = secret()
    supplied = request.headers.get("X-Owner-Panel-Key", "")
    if not expected or not hmac.compare_digest(supplied, expected):
        return JsonResponse({"error": "Forbidden"}, status=403)
    if len(request.body) > 16384:
        return JsonResponse({"error": "Request too large"}, status=413)
    try:
        if action == "create-token" and request.method == "POST":
            result, status = create_token(json.loads(request.body))
            response = JsonResponse(result, status=status)
        elif action == "set-payout" and request.method == "POST":
            result, status = set_payout(json.loads(request.body))
            response = JsonResponse(result, status=status)
        elif action == "update-token" and request.method == "POST":
            result, status = update_token(json.loads(request.body))
            response = JsonResponse(result, status=status)
        elif request.method != "GET":
            return JsonResponse({"error": "Method not allowed"}, status=405)
        elif action == "dashboard":
            response = JsonResponse(panel_data.dashboard(request.GET))
        elif action == "referrals":
            response = JsonResponse(panel_data.referrals())
        elif action == "tokens":
            response = JsonResponse(panel_data.token_list(request.GET))
        elif action == "token":
            pk = int(request.GET["id"])
            if not 0 < pk <= 9223372036854775807:
                raise ValueError("Invalid id")
            token = (
                GhostAccessToken.objects.select_related("user").filter(pk=pk).first()
            )
            if token is None:
                return JsonResponse({"error": "Токен не найден."}, status=404)
            response = JsonResponse(panel_data.token_detail(token))
        elif action == "users":
            from django.db.models import Q

            q = (request.GET.get("q") or "").strip().lstrip("@")
            users = GhostUser.objects.filter(
                Q(name__icontains=q) | Q(telegram_username__icontains=q)
            ).order_by("name", "pk")[:50]
            response = JsonResponse(
                {
                    "users": [
                        dict(id=u.pk, label=panel_data.label(u), name=u.name)
                        for u in users
                    ]
                }
            )
        elif action == "events":
            since = datetime.fromisoformat(request.GET["since"])
            if not timezone.is_aware(since):
                raise ValueError("Timezone required")
            response = JsonResponse({"events": panel_data.events(since)})
        else:
            return JsonResponse({"error": "Not found"}, status=404)
    except (ValueError, TypeError, KeyError, OverflowError, GhostUser.DoesNotExist):
        return JsonResponse({"error": "Некорректные параметры запроса."}, status=400)
    response["Cache-Control"] = "no-store"
    return response
