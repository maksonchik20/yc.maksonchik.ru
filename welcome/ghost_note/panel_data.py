from collections import defaultdict
from decimal import Decimal
from django.db.models import Min
from django.utils import timezone
from .models import (
    GhostUser,
    GhostAccessToken,
    GhostPurchaseOrder,
    GhostReferralCommission,
    GhostSession,
    GhostTelegramBotMessage,
)
from .panel_common import period, money, day, iso, series_for, paginate, payment_totals


def label(user):
    if not user:
        return ""
    return (
        "@" + user.telegram_username.lstrip("@")
        if user.telegram_username
        else user.name
    )


def payment_rows():
    commissions = {
        c.token_id: c
        for c in GhostReferralCommission.objects.select_related("referrer")
    }
    orders = list(
        GhostPurchaseOrder.objects.select_related("user", "referrer", "token")
    )
    linked = {o.token_id for o in orders if o.token_id}
    rows = []
    for o in orders:
        c = commissions.get(o.token_id)
        rows.append(
            dict(
                id=str(o.public_id),
                at=iso(o.paid_at if o.status == "paid" and o.paid_at else o.created_at),
                amount=money(o.amount),
                status=o.status,
                user=label(o.user) or o.customer_telegram or o.customer_name,
                user_id=o.user_id,
                referrer=label(c.referrer if c else o.referrer),
                commission=money(c.commission_amount if c else 0),
                plan="{} · {} мин".format(
                    o.get_access_type_display(), o.duration_minutes
                ),
                source="ЮKassa",
                admin_url="https://yc.maksonchik.ru/admin/ghost_note/ghostpurchaseorder/{}/change/".format(
                    o.pk
                ),
            )
        )
    for t in (
        GhostAccessToken.objects.filter(token_type="real")
        .exclude(pk__in=linked)
        .select_related("user__referred_by")
    ):
        c = commissions.get(t.pk)
        rows.append(
            dict(
                id="token-{}".format(t.pk),
                at=iso(t.created_at),
                amount=money(t.payment_amount),
                status="paid",
                user=label(t.user) or t.label,
                user_id=t.user_id,
                referrer=label(
                    c.referrer if c else (t.user.referred_by if t.user else None)
                ),
                commission=money(c.commission_amount if c else 0),
                plan="Реальный токен",
                source="Вручную",
                admin_url="https://yc.maksonchik.ru/admin/ghost_note/ghostaccesstoken/{}/change/".format(
                    t.pk
                ),
            )
        )
    return rows, orders


def dashboard(params):
    first, last, begin, end = period(params)
    now = timezone.now()
    series = series_for(first, last)
    from datetime import datetime

    all_rows, orders = payment_rows()
    rows = [r for r in all_rows if begin <= datetime.fromisoformat(r["at"]) < end]
    for r in rows:
        if r["status"] == "paid":
            s = series[day(datetime.fromisoformat(r["at"]))]
            s["revenue"] += float(r["amount"])
            s["payments"] += 1
            if r["referrer"]:
                s["referral_revenue"] += float(r["amount"])
    for o in orders:
        at = o.checkout_at or (
            o.created_at if o.yookassa_payment_id and o.status != "failed" else None
        )
        if day(at) in series:
            series[day(at)]["checkouts"] += 1
    for created in GhostUser.objects.filter(
        created_at__gte=begin, created_at__lt=end
    ).values_list("created_at", flat=True):
        series[day(created)]["new_users"] += 1
    starts = defaultdict(set)
    for created, uid, text in GhostTelegramBotMessage.objects.filter(
        direction="in",
        created_at__gte=begin,
        created_at__lt=end,
        text__startswith="/start",
    ).values_list("created_at", "telegram_user_id", "text"):
        command = text.split()[0].split("@")[0] if text.split() else ""
        if command == "/start":
            starts[day(created)].add(uid)
    for key in series:
        series[key]["started"] = len(starts[key])
    # First actual program session for each known owner, not token creation.
    for r in (
        GhostSession.objects.exclude(access_token__user=None)
        .values("access_token__user_id")
        .annotate(first=Min("created_at"))
    ):
        if day(r["first"]) in series:
            series[day(r["first"])]["connected"] += 1
    for expiry in GhostAccessToken.objects.filter(
        token_type="real",
        expires_at__gte=begin,
        expires_at__lt=end,
        expires_at__lte=now,
    ).values_list("expires_at", flat=True):
        series[day(expiry)]["expired"] += 1
    totals = payment_totals(rows)
    totals.update(
        users_total=GhostUser.objects.count(),
        connected_total=GhostSession.objects.exclude(access_token__user=None)
        .values("access_token__user")
        .distinct()
        .count(),
        checkout_count=sum(s["checkouts"] for s in series.values()),
        upcoming_tokens=GhostAccessToken.objects.filter(
            token_type="real", is_active=True, starts_at__gt=now
        ).count(),
        active_tokens=GhostAccessToken.objects.filter(
            token_type="real", is_active=True, starts_at__lte=now, expires_at__gt=now
        ).count(),
    )
    return dict(
        project="ghost",
        generated_at=iso(now),
        summary=totals,
        series=list(series.values()),
        payments=paginate(sorted(rows, key=lambda r: r["at"], reverse=True), params),
        definitions="Новые подключения — первый запуск программы пользователем. /start — уникальные пользователи за день. Отключения программы не фиксируются. Время — Москва.",
        checkout_note="До запуска панели переходы восстановлены по созданным ссылкам. Реальные токены, созданные вручную, учитываются отдельно от заказов; тестовые токены исключены.",
    )


def payout_revision(commission):
    """Reject a checkbox saved from an outdated amount, recipient or paid state."""
    import hashlib
    import json

    values = [
        commission.pk,
        commission.referrer_id,
        commission.referred_user_id,
        commission.token_id,
        money(commission.payment_amount),
        money(commission.commission_amount),
        commission.is_paid,
    ]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def referrals():
    users = list(GhostUser.objects.select_related("referred_by"))
    by_id = {u.pk: u for u in users}
    earned, paid, buyer = (
        defaultdict(Decimal),
        defaultdict(Decimal),
        defaultdict(Decimal),
    )
    payouts = []
    for c in GhostReferralCommission.objects.order_by("-created_at", "-pk"):
        earned[c.referrer_id] += c.commission_amount
        buyer[c.referred_user_id] += c.commission_amount
        if c.is_paid:
            paid[c.referrer_id] += c.commission_amount
        payouts.append(
            dict(
                id=c.pk,
                referrer_id=c.referrer_id,
                user=label(by_id.get(c.referrer_id)),
                buyer=label(by_id.get(c.referred_user_id)),
                amount=money(c.commission_amount),
                payment_amount=money(c.payment_amount),
                is_paid=c.is_paid,
                at=iso(c.created_at),
                token_id=c.token_id,
                revision=payout_revision(c),
                admin_url="https://yc.maksonchik.ru/admin/ghost_note/ghostreferralcommission/{}/change/".format(
                    c.pk
                ),
            )
        )
    amounts = defaultdict(Decimal)
    counts = defaultdict(int)
    for row in payment_rows()[0]:
        if row["status"] == "paid" and row["user_id"]:
            amounts[row["user_id"]] += Decimal(row["amount"])
            counts[row["user_id"]] += 1
    partners = [
        dict(
            id=pk,
            user=label(by_id.get(pk)),
            earned=money(total),
            paid=money(paid[pk]),
            due=money(total - paid[pk]),
            reserved="0.00",
            available=money(total - paid[pk]),
            admin_url="https://yc.maksonchik.ru/admin/ghost_note/ghostuser/{}/change/".format(
                pk
            ),
        )
        for pk, total in earned.items()
    ]
    invitees = [
        dict(
            id=u.pk,
            user=label(u),
            referrer=label(u.referred_by),
            purchases=counts[u.pk],
            revenue=money(amounts[u.pk]),
            commission=money(buyer[u.pk]),
            admin_url="https://yc.maksonchik.ru/admin/ghost_note/ghostuser/{}/change/".format(
                u.pk
            ),
        )
        for u in users
        if u.referred_by_id
    ]
    return dict(
        payouts=payouts,
        partners=sorted(partners, key=lambda x: Decimal(x["due"]), reverse=True),
        requests=[],
        referrals=sorted(invitees, key=lambda x: Decimal(x["revenue"]), reverse=True),
        totals=dict(
            earned=money(sum(earned.values())),
            paid=money(sum(paid.values())),
            due=money(sum(earned.values()) - sum(paid.values())),
            reserved="0.00",
        ),
    )


def token_status(token, now=None):
    now = now or timezone.now()
    if not token.is_active:
        return "disabled"
    if token.starts_at > now:
        return "future"
    return "expired" if token.expires_at <= now else "active"


def token_revision(token):
    import hashlib
    import json

    # Usage timestamps change on every connection and are not editable here.
    values = [
        token.pk,
        token.user_id,
        token.token,
        token.token_type,
        money(token.payment_amount) if token.payment_amount is not None else None,
        token.label,
        iso(token.starts_at),
        iso(token.expires_at),
        token.is_active,
        token.allow_local,
        token.allow_remote,
        token.allow_ai,
    ]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def token_detail(token):
    from .session_links import owner_session_data
    fieldToDetail = dict(
        id=token.pk,
        user=label(token.user) or token.label or "Без пользователя",
        token=token.token,
        token_type=token.token_type,
        amount=money(token.payment_amount),
        label=token.label,
        is_active=token.is_active,
        status=token_status(token),
        starts_at=timezone.localtime(token.starts_at).isoformat(),
        expires_at=timezone.localtime(token.expires_at).isoformat(),
        allow_local=token.allow_local,
        allow_remote=token.allow_remote,
        allow_ai=token.allow_ai,
        created_at=iso(token.created_at),
        last_used_at=iso(token.last_used_at),
        revision=token_revision(token),
    )
    fieldToDetail.update(owner_session_data(token))
    return fieldToDetail


def token_list(params):
    now = timezone.now()
    rows = []
    query = GhostAccessToken.objects.select_related("user").order_by("starts_at", "pk")
    kind = params.get("type") or "real"
    if kind in ("real", "test"):
        query = query.filter(token_type=kind)
    for t in query:
        status = (
            "disabled"
            if not t.is_active
            else (
                "future"
                if t.starts_at > now
                else ("expired" if t.expires_at <= now else "active")
            )
        )
        rows.append(
            dict(
                id=t.pk,
                user=label(t.user) or t.label,
                label=t.label,
                status=status,
                token_type=t.token_type,
                starts_at=iso(t.starts_at),
                expires_at=iso(t.expires_at),
                amount=money(t.payment_amount),
                allow_local=t.allow_local,
                allow_remote=t.allow_remote,
                allow_ai=t.allow_ai,
                admin_url="https://yc.maksonchik.ru/admin/ghost_note/ghostaccesstoken/{}/change/".format(
                    t.pk
                ),
            )
        )
    if params.get("status") != "future":
        rows.reverse()
    return paginate(rows, params)


def events(since):
    now = timezone.now()
    result = []

    def add(key, kind, at, title, body, tab="payments"):
        result.append(
            dict(
                key=key,
                category=kind,
                occurred_at=iso(at),
                payload=dict(
                    title=title, body=body, url="/panel/?project=ghost&tab=" + tab
                ),
            )
        )

    for o in GhostPurchaseOrder.objects.filter(
        checkout_at__gte=since, checkout_at__lte=now
    ).select_related("user"):
        add(
            "ghost-checkout-{}".format(o.pk),
            "ghost.checkout",
            o.checkout_at,
            "GhostNote · переход к оплате",
            "{} · {} ₽".format(
                label(o.user) or o.customer_telegram or o.customer_name, money(o.amount)
            ),
        )
    for o in GhostPurchaseOrder.objects.filter(
        status="paid", paid_at__gte=since, paid_at__lte=now
    ).select_related("user"):
        add(
            "ghost-paid-{}".format(o.pk),
            "ghost.paid",
            o.paid_at,
            "GhostNote · новая оплата",
            "{} · {} ₽".format(
                label(o.user) or o.customer_telegram or o.customer_name, money(o.amount)
            ),
        )
    for t in GhostAccessToken.objects.filter(
        token_type="real", is_active=True, expires_at__gte=since, expires_at__lte=now
    ).select_related("user"):
        add(
            "ghost-expired-{}-{}".format(t.pk, int(t.expires_at.timestamp())),
            "ghost.expired",
            t.expires_at,
            "GhostNote · токен закончился",
            "{} · токен №{}".format(label(t.user) or t.label, t.pk),
            "tokens",
        )
    for t in GhostAccessToken.objects.filter(
        token_type="real",
        created_at__gte=since,
        created_at__lte=now,
        purchase_order__isnull=True,
    ).select_related("user"):
        if t.payment_amount:
            add(
                "ghost-manual-paid-{}".format(t.pk),
                "ghost.paid",
                t.created_at,
                "GhostNote · оплата вручную",
                "{} · {} ₽".format(label(t.user) or t.label, money(t.payment_amount)),
            )
    return result
