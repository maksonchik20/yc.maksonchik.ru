from decimal import Decimal

from django.db.models import Count
from django import forms
from django.contrib import admin
from django.contrib.admin import widgets as admin_widgets
from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.utils.html import format_html
from django.utils import timezone

from .auth import format_token_datetime
from .models import (
    GhostAccessToken,
    GhostReferralCommission,
    GhostSession,
    GhostTextMessage,
    GhostUser,
)

MSK_DATE_INPUT_FORMATS = ['%d.%m.%Y', '%Y-%m-%d']
MSK_TIME_INPUT_FORMATS = ['%H:%M', '%H:%M:%S']


class GhostAccessTokenAdminForm(forms.ModelForm):
    starts_at = forms.SplitDateTimeField(
        label='Действителен с',
        required=False,
        widget=admin_widgets.AdminSplitDateTime(),
        input_date_formats=MSK_DATE_INPUT_FORMATS,
        input_time_formats=MSK_TIME_INPUT_FORMATS,
    )
    expires_at = forms.SplitDateTimeField(
        label='Действителен до',
        required=False,
        widget=admin_widgets.AdminSplitDateTime(),
        input_date_formats=MSK_DATE_INPUT_FORMATS,
        input_time_formats=MSK_TIME_INPUT_FORMATS,
    )

    class Meta:
        model = GhostAccessToken
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        token_type = self.initial.get('token_type') or getattr(self.instance, 'token_type', None)
        if token_type == GhostAccessToken.TokenType.TEST:
            self.fields['payment_amount'].widget = forms.HiddenInput()
            self.fields['payment_amount'].required = False
            self.fields['starts_at'].disabled = True
            self.fields['expires_at'].disabled = True
            self.fields['starts_at'].help_text = 'Для тестового токена выставляется автоматически: сейчас.'
            self.fields['expires_at'].help_text = 'Для тестового токена: сейчас + 1,5 часа.'
        else:
            self.fields['payment_amount'].required = True

    def clean(self):
        cleaned = super().clean()
        allow_local = cleaned.get('allow_local')
        allow_remote = cleaned.get('allow_remote')
        if allow_local is False and allow_remote is False:
            raise ValidationError('Выберите хотя бы один вариант использования: локальный или удалённый.')

        token_type = cleaned.get('token_type')
        starts_at = cleaned.get('starts_at')
        expires_at = cleaned.get('expires_at')
        if token_type == GhostAccessToken.TokenType.TEST:
            now = timezone.now()
            cleaned['starts_at'] = now
            cleaned['expires_at'] = now + timezone.timedelta(hours=1, minutes=30)
            cleaned['payment_amount'] = None
        elif token_type == GhostAccessToken.TokenType.REAL:
            payment_amount = cleaned.get('payment_amount')
            if payment_amount is None:
                self.add_error('payment_amount', 'Укажите сумму оплаты для реального токена.')
            if starts_at and expires_at and starts_at >= expires_at:
                raise ValidationError('Время начала должно быть раньше времени окончания.')
        return cleaned


class ReferralUserInline(admin.TabularInline):
    model = GhostUser
    fk_name = 'referred_by'
    extra = 0
    fields = ('name', 'telegram_link_display', 'total_paid_display', 'commission_display')
    readonly_fields = ('name', 'telegram_link_display', 'total_paid_display', 'commission_display')
    verbose_name = 'Приглашённый пользователь'
    verbose_name_plural = 'Приглашённые пользователи'
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description='Telegram')
    def telegram_link_display(self, obj):
        return _format_telegram_link(obj)

    @admin.display(description='Сумма покупок')
    def total_paid_display(self, obj):
        total = obj.tokens.filter(token_type=GhostAccessToken.TokenType.REAL).aggregate(
            total=Sum('payment_amount')
        )['total']
        return _format_money(total)

    @admin.display(description='Комиссия 20%')
    def commission_display(self, obj):
        if not obj.referred_by_id:
            return '—'
        total = obj.referred_by.commissions_earned.filter(referred_user=obj).aggregate(
            total=Sum('commission_amount')
        )['total']
        return _format_money(total)


def _format_money(value):
    if value in (None, ''):
        return '0 ₽'
    return f'{Decimal(value):,.2f} ₽'.replace(',', ' ')


def _format_telegram_link(user):
    if not user or not user.telegram_username:
        return '—'
    username = user.telegram_username.lstrip('@')
    return format_html(
        '<a href="https://t.me/{}" target="_blank" rel="noopener">@{}</a>',
        username,
        username,
    )


@admin.register(GhostUser)
class GhostUserAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'telegram_link_display',
        'referred_by',
        'referrals_count',
        'total_commission_display',
        'created_at_msk',
    )
    list_filter = ('referred_by',)
    search_fields = ('name', 'telegram_username', 'notes')
    autocomplete_fields = ('referred_by',)
    readonly_fields = ('created_at_msk', 'referral_stats_table', 'total_commission_display', 'telegram_link_display')
    inlines = (ReferralUserInline, GhostAccessTokenInline)
    fieldsets = (
        (None, {
            'fields': (
                'name',
                'telegram_username',
                'telegram_link_display',
                'referred_by',
                'notes',
            ),
        }),
        ('Реферальная программа', {
            'fields': ('referral_stats_table', 'total_commission_display'),
        }),
        ('Служебное', {
            'fields': ('created_at_msk',),
        }),
    )

    class Media:
        css = {
            'all': ('ghost_note/admin/referrals.css',),
        }

    @admin.display(description='Telegram')
    def telegram_link_display(self, obj):
        return _format_telegram_link(obj)

    @admin.display(description='Приглашено')
    def referrals_count(self, obj):
        return getattr(obj, '_referrals_count', obj.referrals.count())

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_referrals_count=Count('referrals'))

    @admin.display(description='Комиссия всего')
    def total_commission_display(self, obj):
        if not obj or not obj.pk:
            return '0 ₽'
        return _format_money(obj.total_commission())

    @admin.display(description='Создан', ordering='created_at')
    def created_at_msk(self, obj):
        if not obj or not obj.created_at:
            return '—'
        return format_token_datetime(obj.created_at)

    @admin.display(description='Рефералы и доход')
    def referral_stats_table(self, obj):
        if not obj or not obj.pk:
            return '—'

        referrals = obj.referrals.order_by('name')
        if not referrals.exists():
            return format_html('<p class="help">Пока никого не пригласил.</p>')

        rows = []
        for referral in referrals:
            paid = referral.tokens.filter(token_type=GhostAccessToken.TokenType.REAL).aggregate(
                total=Sum('payment_amount')
            )['total'] or Decimal('0.00')
            commission = obj.commissions_earned.filter(referred_user=referral).aggregate(
                total=Sum('commission_amount')
            )['total'] or Decimal('0.00')
            rows.append(format_html(
                '<tr>'
                '<td><a href="{}">{}</a></td>'
                '<td>{}</td>'
                '<td>{}</td>'
                '<td>{}</td>'
                '</tr>',
                f'../../ghostuser/{referral.pk}/change/',
                referral.name,
                _format_telegram_link(referral),
                _format_money(paid),
                _format_money(commission),
            ))

        return format_html(
            '<table class="ghost-referral-table">'
            '<thead><tr>'
            '<th>Пользователь</th>'
            '<th>Telegram</th>'
            '<th>Сумма покупок</th>'
            '<th>Комиссия 20%</th>'
            '</tr></thead>'
            '<tbody>{}</tbody>'
            '<tfoot><tr>'
            '<th colspan="3">Итого</th>'
            '<th>{}</th>'
            '</tr></tfoot>'
            '</table>',
            format_html(''.join(str(row) for row in rows)),
            _format_money(obj.total_commission()),
        )


class GhostAccessTokenInline(admin.TabularInline):
    model = GhostAccessToken
    extra = 0
    fields = (
        'token_type',
        'token',
        'payment_amount',
        'starts_at_msk',
        'expires_at_msk',
        'is_active',
        'viewer_link',
    )
    readonly_fields = ('token', 'starts_at_msk', 'expires_at_msk', 'viewer_link')
    show_change_link = True

    @admin.display(description='Действителен с')
    def starts_at_msk(self, obj):
        return format_token_datetime(obj.starts_at)

    @admin.display(description='Действителен до')
    def expires_at_msk(self, obj):
        return format_token_datetime(obj.expires_at)

    def viewer_link(self, obj):
        from urllib.parse import quote

        if not obj.token:
            return '—'
        return format_html(
            '<a href="/ghost/viewer/?token={}" target="_blank">viewer</a>',
            quote(obj.token, safe=''),
        )

    viewer_link.short_description = 'Viewer'


@admin.register(GhostAccessToken)
class GhostAccessTokenAdmin(admin.ModelAdmin):
    form = GhostAccessTokenAdminForm
    list_display = (
        'token_preview',
        'user',
        'token_type',
        'payment_amount',
        'allow_local',
        'allow_remote',
        'starts_at_msk',
        'expires_at_msk',
        'is_active',
        'last_used_at_msk',
    )
    list_filter = ('token_type', 'is_active', 'allow_local', 'allow_remote', 'user')
    search_fields = ('token', 'label', 'user__name', 'user__telegram_username')
    autocomplete_fields = ('user',)
    readonly_fields = ('token', 'created_at_msk', 'last_used_at_msk', 'viewer_link')
    fieldsets = (
        (None, {
            'fields': (
                'user',
                'token_type',
                'payment_amount',
                'label',
                'token',
                'starts_at',
                'expires_at',
                'is_active',
                'allow_local',
                'allow_remote',
                'viewer_link',
            ),
        }),
        ('Служебное', {
            'fields': ('created_at_msk', 'last_used_at_msk'),
        }),
    )

    def token_preview(self, obj):
        return obj.token

    token_preview.short_description = 'Токен'

    @admin.display(description='Действителен с', ordering='starts_at')
    def starts_at_msk(self, obj):
        return format_token_datetime(obj.starts_at)

    @admin.display(description='Действителен до', ordering='expires_at')
    def expires_at_msk(self, obj):
        return format_token_datetime(obj.expires_at)

    @admin.display(description='Последнее использование', ordering='last_used_at')
    def last_used_at_msk(self, obj):
        if not obj or not obj.last_used_at:
            return '—'
        return format_token_datetime(obj.last_used_at)

    @admin.display(description='Создан', ordering='created_at')
    def created_at_msk(self, obj):
        if not obj or not obj.created_at:
            return '—'
        return format_token_datetime(obj.created_at)

    def viewer_link(self, obj):
        from urllib.parse import quote

        if not obj.token:
            return '—'
        return format_html(
            '<a href="/ghost/viewer/?token={}" target="_blank">/ghost/viewer/?token=…</a>',
            quote(obj.token, safe=''),
        )

    viewer_link.short_description = 'Ссылка viewer'

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        now = timezone.now()
        initial.setdefault('token_type', GhostAccessToken.TokenType.TEST)
        initial.setdefault('starts_at', now)
        initial.setdefault('expires_at', now + timezone.timedelta(hours=1, minutes=30))
        return initial

    def save_model(self, request, obj, form, change):
        if obj.token_type == GhostAccessToken.TokenType.TEST:
            obj.apply_test_token_schedule()
            obj.payment_amount = None
        obj.sync_label_from_user()
        super().save_model(request, obj, form, change)


@admin.register(GhostReferralCommission)
class GhostReferralCommissionAdmin(admin.ModelAdmin):
    list_display = (
        'referrer',
        'referred_user',
        'token',
        'payment_amount',
        'commission_amount',
        'created_at_msk',
    )
    list_filter = ('referrer', 'referred_user')
    search_fields = ('referrer__name', 'referred_user__name', 'token__token')
    readonly_fields = (
        'referrer',
        'referred_user',
        'token',
        'payment_amount',
        'commission_amount',
        'created_at_msk',
    )

    @admin.display(description='Создано', ordering='created_at')
    def created_at_msk(self, obj):
        return format_token_datetime(obj.created_at)

    def has_add_permission(self, request):
        return False


@admin.register(GhostSession)
class GhostSessionAdmin(admin.ModelAdmin):
    list_display = ('session_id', 'access_token', 'screenshot_updated_at', 'created_at')
    readonly_fields = ('session_id', 'created_at', 'updated_at', 'screenshot_updated_at')
    search_fields = ('session_id',)
    list_filter = ('access_token',)


@admin.register(GhostTextMessage)
class GhostTextMessageAdmin(admin.ModelAdmin):
    list_display = ('session', 'text_preview', 'delivered', 'created_at')
    list_filter = ('delivered',)
    search_fields = ('text', 'session__session_id')

    def text_preview(self, obj):
        return obj.text[:80] + ('…' if len(obj.text) > 80 else '')

    text_preview.short_description = 'Text'
