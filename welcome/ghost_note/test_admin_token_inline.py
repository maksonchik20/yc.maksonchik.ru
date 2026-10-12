from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import GhostAccessToken, GhostReferralCommission, GhostUser, TEST_TOKEN_DURATION


class GhostTokenInlineTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.operator = get_user_model().objects.create_superuser(
            username='token-admin-test', password='test-only', email='test@example.test'
        )
        cls.referrer = GhostUser.objects.create(name='Inviter test')
        cls.user = GhostUser.objects.create(name='Token owner test', referred_by=cls.referrer)

    def setUp(self):
        self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('External HTTP forbidden in tests'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.client.force_login(self.operator)
        self.url = reverse('admin:ghost_note_ghostuser_change', args=[self.user.pk])
        self.start = timezone.make_aware(datetime(2026, 9, 15, 15, 0))
        self.end = self.start + timedelta(days=30)

    def token_data(self, **overrides):
        result = dict(token_type='real', payment_amount='1000.00',
                      starts_at_0='15.09.2026', starts_at_1='15:00:00',
                      expires_at_0='15.10.2026', expires_at_1='15:00:00',
                      is_active='on', allow_local='on', allow_remote='on')
        result.update(overrides)
        return result

    def user_post(self, rows, initial=0, parent=None):
        data = dict(name=self.user.name, telegram_username='', notes='', referred_by=self.referrer.pk)
        data.update(parent or {})
        data.update({'referrals-TOTAL_FORMS': '0', 'referrals-INITIAL_FORMS': '0',
                     'tokens-TOTAL_FORMS': str(len(rows)), 'tokens-INITIAL_FORMS': str(initial),
                     '_continue': '1'})
        for index, row in enumerate(rows):
            data.update({'tokens-{}-{}'.format(index, key): value for key, value in row.items()})
        return self.client.post(self.url, data)

    def test_create_real_token_from_user_page(self):
        response = self.user_post([self.token_data()])
        self.assertEqual(response.status_code, 302)
        token = self.user.tokens.get()
        self.assertEqual(token.starts_at, self.start)
        self.assertEqual(token.expires_at, self.end)
        self.assertEqual(token.payment_amount, Decimal('1000.00'))
        self.assertTrue(token.token)
        self.assertEqual(token.label, self.user.name)
        commission = GhostReferralCommission.objects.get(token=token)
        self.assertEqual(commission.commission_amount, Decimal('200.00'))

    def test_create_test_token_without_dates_or_payment(self):
        before = timezone.now()
        response = self.user_post([self.token_data(token_type='test', payment_amount='',
            starts_at_0='', starts_at_1='', expires_at_0='', expires_at_1='')])
        self.assertEqual(response.status_code, 302)
        token = self.user.tokens.get()
        self.assertIsNone(token.payment_amount)
        self.assertGreaterEqual(token.starts_at, before)
        self.assertEqual(token.expires_at - token.starts_at, TEST_TOKEN_DURATION)
        self.assertFalse(GhostReferralCommission.objects.filter(token=token).exists())

    def test_real_token_requires_both_dates_and_payment(self):
        for missing in ('starts_at', 'expires_at', 'payment_amount'):
            with self.subTest(missing=missing):
                overrides = {missing: ''} if missing == 'payment_amount' else {missing+'_0': '', missing+'_1': ''}
                response = self.user_post([self.token_data(**overrides)])
                self.assertEqual(response.status_code, 200)
                token_inline = next(i for i in response.context['inline_admin_formsets'] if i.formset.prefix == 'tokens')
                self.assertIn(missing, token_inline.formset.forms[0].errors)
                self.assertFalse(self.user.tokens.exists())

    def test_reversed_dates_show_validation_error(self):
        response = self.user_post([self.token_data(expires_at_0='14.09.2026')])
        self.assertContains(response, 'Время начала должно быть раньше времени окончания.')
        self.assertFalse(self.user.tokens.exists())

    def test_at_least_one_access_mode_is_required(self):
        response = self.user_post([self.token_data(allow_local='', allow_remote='')])
        self.assertContains(response, 'Выберите хотя бы один вариант использования')
        self.assertFalse(self.user.tokens.exists())

    def test_custom_test_dates_and_access_mode_are_preserved(self):
        response = self.user_post([self.token_data(token_type='test', payment_amount='', allow_remote='')])
        self.assertEqual(response.status_code, 302)
        token = self.user.tokens.get()
        self.assertEqual((token.starts_at, token.expires_at), (self.start, self.end))
        self.assertTrue(token.allow_local)
        self.assertFalse(token.allow_remote)

    def test_existing_real_row_does_not_make_new_test_payment_required(self):
        token = GhostAccessToken.objects.create(user=self.user, starts_at=self.start, expires_at=self.end,
                                               payment_amount=Decimal('1000.00'), allow_remote=False)
        response = self.user_post([
            self.token_data(id=token.pk, allow_remote=''),
            self.token_data(token_type='test', payment_amount='', starts_at_0='', starts_at_1='', expires_at_0='', expires_at_1=''),
        ], initial=1)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.user.tokens.count(), 2)
        token.refresh_from_db()
        self.assertEqual((token.starts_at, token.expires_at), (self.start, self.end))
        self.assertFalse(token.allow_remote)
        self.assertEqual(GhostReferralCommission.objects.filter(token=token).count(), 1)

    def test_standalone_token_add_still_works(self):
        data = self.token_data(user=self.user.pk)
        response = self.client.post(reverse('admin:ghost_note_ghostaccesstoken_add'), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.user.tokens.get().expires_at, self.end)

    def test_user_page_includes_editable_inline_dates_and_calendars(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'name="tokens-__prefix__-starts_at_0"')
        self.assertContains(response, 'name="tokens-__prefix__-expires_at_0"')
        self.assertContains(response, 'DateTimeShortcuts.js')

    def test_invalid_inline_does_not_save_parent_changes(self):
        response = self.user_post([self.token_data(expires_at_0='', expires_at_1='')], parent={'name': 'Must not persist'})
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.name, 'Token owner test')
        self.assertEqual(self.user.referred_by_id, self.referrer.pk)
        self.assertFalse(self.user.tokens.exists())
