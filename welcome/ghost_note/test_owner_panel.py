import json
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.utils import timezone
from .models import (
    GhostUser,
    GhostAccessToken,
    GhostPurchaseOrder,
    GhostReferralCommission,
    GhostPanelTokenRequest,
)
from . import panel_data


@override_settings(
    OWNER_PANEL_BRIDGE_TOKEN="isolated-test-secret",
    GHOST_NOTE_BOT_TOKEN="",
    GHOST_NOTE_ADMIN_CHAT_ID="",
)
class OwnerPanelTests(TestCase):
    def setUp(self):
        self.partner = GhostUser.objects.create(
            name="Partner", telegram_username="partner"
        )
        self.user = GhostUser.objects.create(
            name="Buyer", telegram_username="buyer", referred_by=self.partner
        )
        self.now = timezone.now()

    def post(self, data, auth=True):
        return self.client.post(
            "/ghost/owner-api/create-token/",
            data=json.dumps(data),
            content_type="application/json",
            HTTP_X_OWNER_PANEL_KEY="isolated-test-secret" if auth else "",
        )

    def payload(self, **kw):
        data = dict(
            request_id=str(uuid.uuid4()),
            user_id=self.user.pk,
            actor="test-owner",
            token_type="real",
            amount="1000.00",
            starts_at=(self.now + timedelta(hours=1)).isoformat(),
            expires_at=(self.now + timedelta(hours=2)).isoformat(),
            allow_local=True,
            allow_remote=False,
        )
        data.update(kw)
        return data

    def token(self, **kw):
        data = dict(
            user=self.user,
            token_type="real",
            payment_amount=1000,
            starts_at=self.now - timedelta(hours=2),
            expires_at=self.now - timedelta(hours=1),
        )
        data.update(kw)
        return GhostAccessToken.objects.create(**data)

    def test_bridge_requires_secret(self):
        self.assertEqual(self.post(self.payload(), auth=False).status_code, 403)
        self.assertEqual(
            self.client.get("/ghost/owner-api/dashboard/").status_code, 403
        )
        self.assertEqual(GhostAccessToken.objects.count(), 0)

    def test_real_creation_and_idempotency(self):
        data = self.payload()
        response = self.post(data)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertIn("no-store", response["Cache-Control"])
        again = self.post(data)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(response.json(), again.json())
        self.assertEqual(GhostAccessToken.objects.count(), 1)
        self.assertEqual(GhostPanelTokenRequest.objects.count(), 1)
        self.assertEqual(GhostReferralCommission.objects.count(), 1)
        token = GhostAccessToken.objects.get()
        self.assertEqual(token.payment_amount, Decimal("1000"))
        self.assertFalse(token.allow_remote)
        self.assertEqual(self.post(dict(data, amount="1100")).status_code, 409)
        self.assertEqual(GhostAccessToken.objects.count(), 1)

    def test_test_token_defaults_twenty_minutes(self):
        response = self.post(
            self.payload(token_type="test", amount="", starts_at=None, expires_at=None)
        )
        self.assertEqual(response.status_code, 201, response.content)
        t = GhostAccessToken.objects.get()
        self.assertEqual(t.expires_at - t.starts_at, timedelta(minutes=20))
        self.assertIsNone(t.payment_amount)
        self.assertFalse(GhostReferralCommission.objects.exists())

    def test_invalid_tokens_do_not_leave_records(self):
        for change in [
            dict(amount="0"),
            dict(amount="-1"),
            dict(amount=""),
            dict(expires_at=self.now.isoformat()),
            dict(starts_at=None),
            dict(allow_local=False, allow_remote=False),
            dict(allow_local="false"),
            dict(user_id=99999),
            dict(request_id="bad"),
        ]:
            response = self.post(self.payload(**change))
            self.assertEqual(response.status_code, 400, (change, response.content))
        self.assertEqual(GhostAccessToken.objects.count(), 0)
        self.assertEqual(GhostPanelTokenRequest.objects.count(), 0)
        self.assertEqual(self.post([]).status_code, 400)

    def test_aware_dates_normalize_to_moscow(self):
        response = self.post(
            self.payload(
                starts_at="2026-09-20T10:00:00+03:00",
                expires_at="2026-09-20T11:00:00+03:00",
            )
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(GhostAccessToken.objects.get().starts_at.hour, 7)

    def test_orders_and_tokens_not_double_counted_and_test_excluded(self):
        t = self.token(expires_at=self.now + timedelta(hours=1))
        GhostPurchaseOrder.objects.create(
            user=self.user,
            referrer=self.partner,
            token=t,
            customer_name="Buyer",
            access_type="local",
            duration_minutes=60,
            starts_at=t.starts_at,
            amount=1000,
            status="paid",
            paid_at=self.now,
        )
        self.token(payment_amount=500)
        self.token(token_type="test", payment_amount=None)
        d = panel_data.dashboard({})
        self.assertEqual(d["summary"]["revenue"], "1500.00")
        self.assertEqual(d["summary"]["payment_count"], 2)
        self.assertEqual(d["payments"]["total"], 2)
        self.assertEqual(d["summary"]["referral_revenue"], "1500.00")

    def test_referral_balance_respects_paid_commissions(self):
        t1 = self.token(payment_amount=1000)
        t2 = self.token(payment_amount=500)
        GhostReferralCommission.objects.filter(token=t1).update(is_paid=True)
        result = panel_data.referrals()
        earned = sum(c.commission_amount for c in GhostReferralCommission.objects.all())
        paid = GhostReferralCommission.objects.get(token=t1).commission_amount
        self.assertEqual(Decimal(result["totals"]["earned"]), earned)
        self.assertEqual(Decimal(result["totals"]["due"]), earned - paid)
        self.assertEqual(result["referrals"][0]["revenue"], "1500.00")

    def test_expiry_push_only_real_active_and_manual_payment(self):
        valid = self.token()
        self.token(token_type="test")
        self.token(is_active=False)
        self.token(expires_at=self.now + timedelta(hours=1))
        result = panel_data.events(self.now - timedelta(hours=2))
        expired = [e for e in result if e["category"] == "ghost.expired"]
        self.assertEqual(len(expired), 1)
        self.assertIn(str(valid.pk), expired[0]["key"])
        paid = [e for e in result if e["category"] == "ghost.paid"]
        self.assertEqual(len(paid), 3)
        self.assertNotIn(valid.token, json.dumps(result))

    def test_checkout_and_payment_events_separate(self):
        GhostPurchaseOrder.objects.create(
            user=self.user,
            customer_name="Buyer",
            access_type="local",
            duration_minutes=60,
            starts_at=self.now,
            amount=1000,
            status="paid",
            paid_at=self.now,
            checkout_at=self.now,
        )
        events = panel_data.events(self.now - timedelta(seconds=1))
        self.assertEqual(
            {e["category"] for e in events}, {"ghost.checkout", "ghost.paid"}
        )

    def test_token_filters_and_no_secret_in_lists(self):
        future = self.token(
            starts_at=self.now + timedelta(hours=1),
            expires_at=self.now + timedelta(hours=2),
        )
        self.token()
        self.token(token_type="test")
        result = panel_data.token_list({"status": "future", "type": "real"})
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["rows"][0]["id"], future.pk)
        self.assertNotIn(future.token, json.dumps(result))

    def test_authorized_views_and_bad_ranges(self):
        for action in ["dashboard", "tokens", "referrals", "users"]:
            result = self.client.get(
                "/ghost/owner-api/" + action + "/",
                HTTP_X_OWNER_PANEL_KEY="isolated-test-secret",
            )
            self.assertEqual(result.status_code, 200, (action, result.content))
            self.assertIn("no-store", result["Cache-Control"])
        result = self.client.get(
            "/ghost/owner-api/dashboard/?from=bad",
            HTTP_X_OWNER_PANEL_KEY="isolated-test-secret",
        )
        self.assertEqual(result.status_code, 400)

    def test_checkout_timestamp_only_after_payment_link_created(self):
        from django.test import RequestFactory
        from django.http import HttpResponse
        from .purchase_views import buy

        order = GhostPurchaseOrder.objects.create(
            user=self.user,
            customer_name="Buyer",
            access_type="local",
            duration_minutes=60,
            starts_at=self.now,
            amount=1000,
        )
        form_data = dict(
            customer_name="Buyer",
            customer_telegram="buyer",
            customer_email="",
            referral_key="",
            access_type="local",
            duration_minutes=60,
            starts_at=self.now,
        )
        with patch("ghost_note.purchase_views.PurchaseForm") as form, patch(
            "ghost_note.purchase_views.create_order_from_form", return_value=order
        ), patch("ghost_note.purchase_views.notify_admin_purchase_attempt"), patch(
            "ghost_note.purchase_views.notify_admin_purchase_failed"
        ), patch(
            "ghost_note.purchase_views.reverse", return_value="/ghost/success/"
        ), patch(
            "ghost_note.purchase_views.render", return_value=HttpResponse(status=502)
        ), patch(
            "ghost_note.purchase_views.create_payment"
        ) as payment:
            form.return_value.is_valid.return_value = True
            form.return_value.cleaned_data = form_data
            payment.return_value = {"id": "test-no-link"}
            self.assertEqual(
                buy(RequestFactory().post("/ghost/buy/", {})).status_code, 502
            )
            order.refresh_from_db()
            self.assertIsNone(order.checkout_at)
            order.status = "pending"
            order.save(update_fields=["status"])
            payment.return_value = {
                "id": "test-with-link",
                "confirmation_url": "https://yookassa.ru/test-only",
            }
            response = buy(RequestFactory().post("/ghost/buy/", {}))
            self.assertEqual(response.status_code, 302)
            order.refresh_from_db()
            self.assertIsNotNone(order.checkout_at)
            self.assertEqual(order.status, "pending")


@override_settings(
    OWNER_PANEL_BRIDGE_TOKEN="isolated-test-secret",
    GHOST_NOTE_BOT_TOKEN="",
    GHOST_NOTE_ADMIN_CHAT_ID="",
)
class PayoutPanelTests(TestCase):
    def setUp(self):
        self.partner = GhostUser.objects.create(
            name="Partner", telegram_username="partner"
        )
        self.buyer = GhostUser.objects.create(
            name="Buyer", telegram_username="buyer", referred_by=self.partner
        )
        for amount in (1000, 500):
            GhostAccessToken.objects.create(
                user=self.buyer,
                token_type="real",
                payment_amount=amount,
                starts_at=timezone.now(),
                expires_at=timezone.now() + timedelta(hours=1),
            )
        self.row = GhostReferralCommission.objects.order_by("pk").first()

    def payload(self, **changes):
        data = dict(
            id=self.row.pk, is_paid=True, revision=panel_data.payout_revision(self.row)
        )
        data.update(changes)
        return data

    def post(self, data, auth=True):
        return self.client.post(
            "/ghost/owner-api/set-payout/",
            data=json.dumps(data),
            content_type="application/json",
            HTTP_X_OWNER_PANEL_KEY="isolated-test-secret" if auth else "",
        )

    def test_payout_rows_show_recipient_buyer_amount_and_existing_state(self):
        self.row.is_paid = True
        self.row.save(update_fields=["is_paid"])
        response = self.client.get(
            "/ghost/owner-api/referrals/", HTTP_X_OWNER_PANEL_KEY="isolated-test-secret"
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(
            data["totals"],
            dict(earned="300.00", paid="200.00", due="100.00", reserved="0.00"),
        )
        row = next(r for r in data["payouts"] if r["id"] == self.row.pk)
        self.assertEqual(
            (
                row["user"],
                row["buyer"],
                row["amount"],
                row["payment_amount"],
                row["is_paid"],
            ),
            ("@partner", "@buyer", "200.00", "1000.00", True),
        )
        for token in GhostAccessToken.objects.all():
            self.assertNotIn(token.token, response.content.decode())

    def test_mark_and_unmark_update_balance_and_only_selected_commission(self):
        tokens = list(GhostAccessToken.objects.values())
        before = self.payload()
        response = self.post(before)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response.json()["data"]["totals"]["due"], "100.00")
        self.row.refresh_from_db()
        self.assertTrue(self.row.is_paid)
        self.assertFalse(
            GhostReferralCommission.objects.exclude(pk=self.row.pk).get().is_paid
        )
        # Replaying an old request cannot flip the checkbox back.
        self.assertEqual(self.post(before).status_code, 409)
        self.row.refresh_from_db()
        self.assertTrue(self.row.is_paid)
        response = self.post(self.payload(is_paid=False))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["totals"]["due"], "300.00")
        self.assertEqual(list(GhostAccessToken.objects.values()), tokens)
        self.row.refresh_from_db()
        self.assertFalse(self.row.is_paid)

    def test_stale_amount_recipient_or_state_cannot_be_overwritten(self):
        for field, value in [
            ("commission_amount", Decimal("199.99")),
            ("referrer_id", self.buyer.pk),
            ("is_paid", True),
        ]:
            self.row.refresh_from_db()
            before = self.payload()
            GhostReferralCommission.objects.filter(pk=self.row.pk).update(
                **{field: value}
            )
            self.assertEqual(self.post(before).status_code, 409)
            self.row.refresh_from_db()
            self.assertEqual(getattr(self.row, field), value)

    def test_auth_method_and_strict_validation(self):
        self.assertEqual(self.post(self.payload(), auth=False).status_code, 403)
        self.assertEqual(
            self.client.get(
                "/ghost/owner-api/set-payout/",
                HTTP_X_OWNER_PANEL_KEY="isolated-test-secret",
            ).status_code,
            404,
        )
        for changes in [
            dict(id=True),
            dict(id="1"),
            dict(id=0),
            dict(id=2**64),
            dict(is_paid="false"),
            dict(is_paid=1),
            dict(is_paid=None),
            dict(revision=None),
            dict(revision="old"),
        ]:
            response = self.post(self.payload(**changes))
            self.assertEqual(response.status_code, 400, changes)
        self.assertEqual(self.post([]).status_code, 400)
        self.assertEqual(self.post(self.payload(id=999999)).status_code, 404)
        self.assertFalse(GhostReferralCommission.objects.filter(is_paid=True).exists())

    def test_failed_response_calculation_rolls_back_mark(self):
        with patch(
            "ghost_note.panel_api.panel_data.referrals",
            side_effect=RuntimeError("test failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.post(self.payload())
        self.row.refresh_from_db()
        self.assertFalse(self.row.is_paid)


@override_settings(
    OWNER_PANEL_BRIDGE_TOKEN="isolated-test-secret",
    GHOST_NOTE_BOT_TOKEN="",
    GHOST_NOTE_ADMIN_CHAT_ID="",
)
class TokenManagementTests(TestCase):
    def setUp(self):
        self.partner = GhostUser.objects.create(name="Partner")
        self.user = GhostUser.objects.create(name="Buyer", referred_by=self.partner)
        now = timezone.now().replace(microsecond=123456)
        self.token = GhostAccessToken.objects.create(
            user=self.user,
            token_type="real",
            payment_amount=1000,
            starts_at=now - timedelta(hours=1),
            expires_at=now + timedelta(hours=1),
        )
        GhostReferralCommission.objects.filter(token=self.token).update(
            is_paid=True, commission_amount=Decimal("123.45")
        )

    def payload(self, **kw):
        detail = panel_data.token_detail(self.token)
        data = {
            k: detail[k]
            for k in (
                "id",
                "revision",
                "label",
                "starts_at",
                "expires_at",
                "is_active",
                "allow_local",
                "allow_remote",
            )
        }
        data.update(kw)
        return data

    def post(self, data, auth=True):
        return self.client.post(
            "/ghost/owner-api/update-token/",
            data=json.dumps(data),
            content_type="application/json",
            HTTP_X_OWNER_PANEL_KEY="isolated-test-secret" if auth else "",
        )

    def test_detail_requires_secret_and_list_does_not_include_token(self):
        self.assertEqual(
            self.client.get(
                "/ghost/owner-api/token/", {"id": self.token.pk}
            ).status_code,
            403,
        )
        r = self.client.get(
            "/ghost/owner-api/token/",
            {"id": self.token.pk},
            HTTP_X_OWNER_PANEL_KEY="isolated-test-secret",
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("no-store", r["Cache-Control"])
        self.assertEqual(r.json()["token"], self.token.token)
        self.assertTrue(r.json()["starts_at"].endswith("+03:00"))
        self.assertIn(".123456", r.json()["starts_at"])
        for section in ("tokens", "referrals", "dashboard"):
            r = self.client.get(
                "/ghost/owner-api/" + section + "/",
                HTTP_X_OWNER_PANEL_KEY="isolated-test-secret",
            )
            self.assertNotIn(self.token.token, r.content.decode())
        self.assertEqual(
            self.client.get(
                "/ghost/owner-api/token/",
                {"id": 999999},
                HTTP_X_OWNER_PANEL_KEY="isolated-test-secret",
            ).status_code,
            404,
        )

    def test_edit_preserves_money_commission_token_user_and_usage(self):
        before = GhostAccessToken.objects.values().get(pk=self.token.pk)
        ledger = list(GhostReferralCommission.objects.values())
        result = self.post(
            self.payload(label="Встреча в 18:00", allow_remote=False, is_active=False)
        )
        self.assertEqual(result.status_code, 200, result.content)
        self.token.refresh_from_db()
        self.assertFalse(self.token.is_valid)
        self.assertFalse(self.token.allow_remote)
        self.assertEqual(self.token.label, "Встреча в 18:00")
        after = GhostAccessToken.objects.values().get(pk=self.token.pk)
        self.assertEqual(
            {
                k: v
                for k, v in before.items()
                if k not in ("label", "allow_remote", "is_active")
            },
            {
                k: v
                for k, v in after.items()
                if k not in ("label", "allow_remote", "is_active")
            },
        )
        self.assertEqual(list(GhostReferralCommission.objects.values()), ledger)
        self.assertEqual(self.post(self.payload(is_active=True)).status_code, 200)
        self.token.refresh_from_db()
        self.assertTrue(self.token.is_valid)
        self.assertEqual(list(GhostReferralCommission.objects.values()), ledger)

    def test_extend_expired_and_change_future_schedule(self):
        GhostAccessToken.objects.filter(pk=self.token.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        self.token.refresh_from_db()
        self.assertEqual(panel_data.token_detail(self.token)["status"], "expired")
        end = timezone.now() + timedelta(hours=2)
        r = self.post(self.payload(expires_at=end.isoformat()))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["status"], "active")
        self.token.refresh_from_db()
        r = self.post(
            self.payload(
                starts_at=(end + timedelta(hours=1)).isoformat(),
                expires_at=(end + timedelta(hours=2)).isoformat(),
            )
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["status"], "future")

    def test_test_token_schedule_is_not_reset_and_no_commission_created(self):
        self.token = GhostAccessToken.objects.create(
            user=self.user,
            token_type="test",
            starts_at=timezone.now() - timedelta(days=1),
            expires_at=timezone.now() - timedelta(days=1) + timedelta(minutes=20),
        )
        original = (self.token.starts_at, self.token.expires_at, self.token.token)
        self.assertEqual(
            self.post(self.payload(label="Тест", is_active=False)).status_code, 200
        )
        self.token.refresh_from_db()
        self.assertEqual(
            (self.token.starts_at, self.token.expires_at, self.token.token), original
        )
        self.assertFalse(
            GhostReferralCommission.objects.filter(token=self.token).exists()
        )

    def test_stale_edits_replays_and_deleted_token_are_rejected(self):
        old = self.payload(label="New label")
        self.assertEqual(self.post(old).status_code, 200)
        self.assertEqual(self.post(old).status_code, 409)
        self.token.refresh_from_db()
        stale = self.payload(label="Overwrite")
        GhostAccessToken.objects.filter(pk=self.token.pk).update(allow_remote=False)
        self.assertEqual(self.post(stale).status_code, 409)
        self.assertEqual(self.post(dict(stale, id=999999)).status_code, 404)
        self.token.refresh_from_db()
        self.assertEqual(self.token.label, "New label")
        self.assertFalse(self.token.allow_remote)

    def test_last_used_does_not_cause_false_conflict(self):
        data = self.payload(label="Usage remains")
        used = timezone.now()
        GhostAccessToken.objects.filter(pk=self.token.pk).update(last_used_at=used)
        self.assertEqual(self.post(data).status_code, 200)
        self.token.refresh_from_db()
        self.assertEqual(self.token.last_used_at, used)

    def test_auth_validation_and_financial_fields_cannot_be_changed(self):
        before = list(GhostAccessToken.objects.values())
        ledger = list(GhostReferralCommission.objects.values())
        self.assertEqual(self.post(self.payload(), auth=False).status_code, 403)
        for extra in (
            dict(payment_amount=2),
            dict(token_type="test"),
            dict(user_id=self.partner.pk),
            dict(token="new"),
        ):
            self.assertEqual(self.post(self.payload(**extra)).status_code, 400)
        for changes in [
            dict(id=True),
            dict(id=2**64),
            dict(revision="bad"),
            dict(is_active="false"),
            dict(allow_local=False, allow_remote=False),
            dict(starts_at="bad"),
            dict(expires_at=None),
            dict(starts_at="2026-09-15T12:00:00"),
            dict(expires_at=self.token.starts_at.isoformat()),
            dict(label="x" * 129),
        ]:
            result = self.post(self.payload(**changes))
            self.assertEqual(result.status_code, 400, (changes, result.content))
        self.assertEqual(self.post([]).status_code, 400)
        self.assertEqual(list(GhostAccessToken.objects.values()), before)
        self.assertEqual(list(GhostReferralCommission.objects.values()), ledger)
