import logging

from decimal import Decimal

from django import forms
from django.contrib import admin
from django.contrib.admin import widgets as admin_widgets
from django.core.exceptions import ValidationError
from django.db.models import Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils import timezone

from .auth import format_token_datetime
from .telegram_notify import is_ghost_bot_configured, send_bot_message
from .bot_logging import format_chat_message_html, resolve_chat_id, strip_html
from .trial import trial_available_at
from .models import (
    GhostAccessToken,
    GhostPurchaseOrder,
    GhostRealPayment,
    GhostReferralCommission,
    GhostReferralPayout,
    GhostSession,
    GhostTelegramBotMessage,
    GhostTelegramContact,
    GhostTextMessage,
    GhostUser,
    TEST_TOKEN_DURATION,
)

logger = logging.getLogger(__name__)

MSK_DATE_INPUT_FORMATS = ['%d.%m.%Y', '%Y-%m-%d']
MSK_TIME_INPUT_FORMATS = ['%H:%M', '%H:%M:%S']
TOKEN_TYPE_CHANGE_HANDLER = (
    'if(window.ghostNoteToggleTokenType)window.ghostNoteToggleTokenType(this,true)'
)


def _apply_token_type_onchange(field):
    field.widget.attrs['onchange'] = TOKEN_TYPE_CHANGE_HANDLER


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

    def _current_token_type(self):
        if self.data.get('token_type'):
            return self.data['token_type']
        if self.initial.get('token_type'):
            return self.initial['token_type']
        if self.instance.pk:
            return self.instance.token_type
        return GhostAccessToken.TokenType.REAL

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        token_type = self._current_token_type()
        self.fields['payment_amount'].label = 'Сумма оплаты'
        self.fields['starts_at'].help_text = (
            'Для тестового токена при выборе типа подставляется автоматически: сейчас. Можно изменить вручную.'
        )
        self.fields['expires_at'].help_text = (
            'Для тестового токена при выборе типа подставляется: сейчас + 20 минут. Можно изменить вручную.'
        )
        if token_type == GhostAccessToken.TokenType.TEST:
            self.fields['payment_amount'].required = False
        else:
            self.fields['payment_amount'].required = True
        _apply_token_type_onchange(self.fields['token_type'])

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
            cleaned['payment_amount'] = None
            now = timezone.now()
            if not starts_at:
                cleaned['starts_at'] = now
            if not expires_at:
                cleaned['expires_at'] = cleaned['starts_at'] + TEST_TOKEN_DURATION
            starts_at = cleaned.get('starts_at')
            expires_at = cleaned.get('expires_at')
            if starts_at and expires_at and starts_at >= expires_at:
                raise ValidationError('Время начала должно быть раньше времени окончания.')
        elif token_type == GhostAccessToken.TokenType.REAL:
            payment_amount = cleaned.get('payment_amount')
            if payment_amount is None:
                self.add_error('payment_amount', 'Укажите сумму оплаты для реального токена.')
            if not starts_at:
                self.add_error('starts_at', 'Укажите дату и время начала.')
            if not expires_at:
                self.add_error('expires_at', 'Укажите дату и время окончания.')
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


class GhostAccessTokenInlineForm(forms.ModelForm):
    class Meta:
        model = GhostAccessToken
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'token_type' in self.fields:
            _apply_token_type_onchange(self.fields['token_type'])


class GhostAccessTokenInline(admin.TabularInline):
    model = GhostAccessToken
    form = GhostAccessTokenInlineForm
    extra = 0
    can_delete = False
    fields = (
        'token_type',
        'token',
        'payment_amount',
        'starts_at_msk',
        'expires_at_msk',
        'is_active',
        'viewer_link',
        'delete_token_link',
    )
    readonly_fields = ('token', 'starts_at_msk', 'expires_at_msk', 'viewer_link', 'delete_token_link')
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

    @admin.display(description='')
    def delete_token_link(self, obj):
        if not obj or not obj.pk:
            return '—'
        url = reverse('admin:ghost_note_ghostaccesstoken_delete', args=[obj.pk])
        return format_html(
            '<a class="ghost-inline-delete-btn" href="{}" title="Удалить токен">'
            '<i class="fas fa-trash-alt"></i>'
            '</a>',
            url,
        )


@admin.register(GhostUser)
class GhostUserAdmin(admin.ModelAdmin):
    change_form_template = 'admin/ghost_note/ghost_admin_change_form.html'
    add_form_template = 'admin/ghost_note/ghost_admin_change_form.html'
    list_display = (
        'name',
        'referral_key',
        'telegram_link_display',
        'referred_by_link',
        'referrals_count',
        'total_commission_display',
        'created_at_msk',
    )
    list_filter = ('referred_by',)
    search_fields = ('name', 'telegram_username', 'notes', 'referral_key')
    autocomplete_fields = ('referred_by',)
    readonly_fields = (
        'referral_key',
        'created_at_msk',
        'referral_stats_table',
        'total_commission_display',
        'telegram_link_display',
    )
    inlines = (ReferralUserInline, GhostAccessTokenInline)
    fieldsets = (
        (None, {
            'fields': (
                'name',
                'referral_key',
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

    @admin.display(description='Кто пригласил', ordering='referred_by__name')
    def referred_by_link(self, obj):
        if not obj.referred_by_id:
            return '—'
        url = reverse('admin:ghost_note_ghostuser_change', args=[obj.referred_by_id])
        return format_html('<a href="{}">{}</a>', url, obj.referred_by.name)

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
                reverse('admin:ghost_note_ghostuser_change', args=[referral.pk]),
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


@admin.register(GhostAccessToken)
class GhostAccessTokenAdmin(admin.ModelAdmin):
    form = GhostAccessTokenAdminForm
    change_form_template = 'admin/ghost_note/ghost_admin_change_form.html'
    add_form_template = 'admin/ghost_note/ghost_admin_change_form.html'
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
        initial.setdefault('token_type', GhostAccessToken.TokenType.REAL)
        initial.setdefault('starts_at', now)
        initial.setdefault('expires_at', now + timezone.timedelta(days=30))
        return initial

    def save_model(self, request, obj, form, change):
        if obj.token_type == GhostAccessToken.TokenType.TEST:
            obj.payment_amount = None
        obj.sync_label_from_user()
        super().save_model(request, obj, form, change)


@admin.register(GhostRealPayment)
class GhostRealPaymentAdmin(admin.ModelAdmin):
    change_list_template = 'admin/ghost_note/ghostrealpayment/change_list.html'
    list_display = (
        'user_link',
        'token_link',
        'payment_amount_display',
        'referred_by_link',
        'created_at_msk',
    )
    list_filter = ('user__referred_by',)
    search_fields = ('token', 'label', 'user__name', 'user__telegram_username')
    ordering = ('-created_at',)

    class Media:
        css = {
            'all': ('ghost_note/admin/referrals.css',),
        }

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .filter(
                token_type=GhostAccessToken.TokenType.REAL,
                payment_amount__isnull=False,
            )
            .select_related('user', 'user__referred_by')
        )

    def changelist_view(self, request, extra_context=None):
        qs = self.get_queryset(request)
        stats = qs.aggregate(
            total=Sum('payment_amount'),
            count=Count('id'),
            unique_users=Count('user_id', distinct=True, filter=Q(user_id__isnull=False)),
        )
        paid_commissions = GhostReferralCommission.objects.filter(is_paid=True).aggregate(
            total=Sum('commission_amount'),
        )['total'] or Decimal('0.00')
        payments_total = stats['total'] or Decimal('0.00')
        profit_total = payments_total - paid_commissions
        extra_context = {
            **(extra_context or {}),
            'payments_total': _format_money(payments_total),
            'payments_unique_users': stats['unique_users'] or 0,
            'payments_count': stats['count'] or 0,
            'payments_paid_commissions': _format_money(paid_commissions),
            'payments_profit': _format_money(profit_total),
        }
        return super().changelist_view(request, extra_context=extra_context)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description='Пользователь', ordering='user__name')
    def user_link(self, obj):
        if not obj.user_id:
            return obj.label or '—'
        url = reverse('admin:ghost_note_ghostuser_change', args=[obj.user_id])
        return format_html('<a href="{}">{}</a>', url, obj.user.name)

    @admin.display(description='Токен', ordering='token')
    def token_link(self, obj):
        url = reverse('admin:ghost_note_ghostaccesstoken_change', args=[obj.pk])
        return format_html('<a href="{}">{}</a>', url, obj.token)

    @admin.display(description='Сумма оплаты', ordering='payment_amount')
    def payment_amount_display(self, obj):
        return _format_money(obj.payment_amount)

    @admin.display(description='Кто пригласил', ordering='user__referred_by__name')
    def referred_by_link(self, obj):
        if not obj.user_id or not obj.user.referred_by_id:
            return '—'
        referrer = obj.user.referred_by
        url = reverse('admin:ghost_note_ghostuser_change', args=[referrer.pk])
        return format_html('<a href="{}">{}</a>', url, referrer.name)

    @admin.display(description='Дата оплаты', ordering='created_at')
    def created_at_msk(self, obj):
        return format_token_datetime(obj.created_at)


@admin.register(GhostReferralPayout)
class GhostReferralPayoutAdmin(admin.ModelAdmin):
    change_list_template = 'admin/ghost_note/ghostreferralpayout/change_list.html'
    list_display = (
        'name_link',
        'telegram_link_display',
        'unpaid_total_display',
        'paid_total_display',
        'unpaid_count_display',
    )
    search_fields = ('name', 'telegram_username')
    ordering = ('name',)

    class Media:
        css = {
            'all': ('ghost_note/admin/referrals.css',),
        }

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(
                _unpaid_total=Coalesce(
                    Sum(
                        'commissions_earned__commission_amount',
                        filter=Q(commissions_earned__is_paid=False),
                    ),
                    Value(Decimal('0.00')),
                    output_field=DecimalField(max_digits=12, decimal_places=2),
                ),
                _paid_total=Coalesce(
                    Sum(
                        'commissions_earned__commission_amount',
                        filter=Q(commissions_earned__is_paid=True),
                    ),
                    Value(Decimal('0.00')),
                    output_field=DecimalField(max_digits=12, decimal_places=2),
                ),
                _unpaid_count=Count(
                    'commissions_earned',
                    filter=Q(commissions_earned__is_paid=False),
                ),
            )
            .filter(_unpaid_total__gt=0)
        )

    def changelist_view(self, request, extra_context=None):
        unpaid_qs = GhostReferralCommission.objects.filter(is_paid=False)
        stats = unpaid_qs.aggregate(
            total=Sum('commission_amount'),
            count=Count('id'),
            users=Count('referrer_id', distinct=True),
        )
        extra_context = {
            **(extra_context or {}),
            'payouts_unpaid_total': _format_money(stats['total']),
            'payouts_users_count': stats['users'] or 0,
            'payouts_unpaid_count': stats['count'] or 0,
        }
        return super().changelist_view(request, extra_context=extra_context)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description='Пользователь', ordering='name')
    def name_link(self, obj):
        url = reverse('admin:ghost_note_ghostuser_change', args=[obj.pk])
        return format_html('<a href="{}">{}</a>', url, obj.name)

    @admin.display(description='Telegram')
    def telegram_link_display(self, obj):
        return _format_telegram_link(obj)

    @admin.display(description='К выплате', ordering='_unpaid_total')
    def unpaid_total_display(self, obj):
        return _format_money(obj._unpaid_total)

    @admin.display(description='Уже выплачено', ordering='_paid_total')
    def paid_total_display(self, obj):
        return _format_money(obj._paid_total)

    @admin.display(description='Невыплаченных', ordering='_unpaid_count')
    def unpaid_count_display(self, obj):
        return obj._unpaid_count


@admin.register(GhostReferralCommission)
class GhostReferralCommissionAdmin(admin.ModelAdmin):
    change_list_template = 'admin/ghost_note/ghostreferralcommission/change_list.html'
    list_display = (
        'referrer',
        'referred_user',
        'token',
        'payment_amount',
        'commission_amount',
        'is_paid_toggle',
        'created_at_msk',
    )
    list_filter = ('is_paid', 'referrer', 'referred_user')
    list_display_links = ('referrer',)
    search_fields = ('referrer__name', 'referred_user__name', 'token__token')
    readonly_fields = (
        'referrer',
        'referred_user',
        'token',
        'payment_amount',
        'commission_amount',
        'created_at_msk',
    )

    @admin.display(description='Выплачено', ordering='is_paid')
    def is_paid_toggle(self, obj):
        url = reverse('admin:ghost_note_ghostreferralcommission_toggle_paid', args=[obj.pk])
        return format_html(
            '<input type="checkbox" class="ghost-commission-paid-toggle" '
            'data-url="{}" aria-label="Выплачено"{}>',
            url,
            format_html(' checked') if obj.is_paid else '',
        )

    @admin.display(description='Создано', ordering='created_at')
    def created_at_msk(self, obj):
        return format_token_datetime(obj.created_at)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                '<int:object_id>/toggle-paid/',
                self.admin_site.admin_view(self.toggle_paid_view),
                name='ghost_note_ghostreferralcommission_toggle_paid',
            ),
        ]
        return custom_urls + urls

    def toggle_paid_view(self, request, object_id):
        if request.method != 'POST':
            return JsonResponse({'ok': False, 'error': 'method_not_allowed'}, status=405)
        if not self.has_change_permission(request):
            return JsonResponse({'ok': False, 'error': 'forbidden'}, status=403)

        commission = get_object_or_404(GhostReferralCommission, pk=object_id)
        is_paid = request.POST.get('is_paid') in ('1', 'true', 'True', 'on')
        commission.is_paid = is_paid
        commission.save(update_fields=['is_paid'])
        return JsonResponse({'ok': True, 'is_paid': commission.is_paid})

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


@admin.register(GhostPurchaseOrder)
class GhostPurchaseOrderAdmin(admin.ModelAdmin):
    list_display = (
        'customer_name',
        'customer_telegram',
        'customer_email',
        'access_type',
        'amount',
        'status',
        'telegram_notified_at',
        'email_notified_at',
        'referrer_notified_at',
        'referral_key_input',
        'token',
        'created_at',
    )
    list_filter = ('status', 'access_type')
    search_fields = (
        'customer_name',
        'customer_telegram',
        'customer_email',
        'referral_key_input',
        'yookassa_payment_id',
        'public_id',
    )
    readonly_fields = (
        'public_id',
        'customer_name',
        'customer_telegram',
        'customer_email',
        'referral_key_input',
        'referrer',
        'user',
        'access_type',
        'duration_minutes',
        'starts_at',
        'amount',
        'yookassa_payment_id',
        'status',
        'token',
        'created_at',
        'paid_at',
        'telegram_notified_at',
        'telegram_notify_error',
        'email_notified_at',
        'email_notify_error',
        'referrer_notified_at',
        'referrer_notify_error',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(GhostTelegramContact)
class GhostTelegramContactAdmin(admin.ModelAdmin):
    change_list_template = 'admin/ghost_note/ghosttelegramcontact/change_list.html'
    list_display = (
        'telegram_user_id',
        'username',
        'last_trial_at',
        'trial_cooldown_status',
        'updated_at',
        'chat_history_link',
        'reset_trial_action',
    )
    search_fields = ('telegram_user_id', 'username')
    readonly_fields = ('telegram_user_id', 'username', 'updated_at', 'trial_cooldown_status')
    fields = ('telegram_user_id', 'username', 'last_trial_at', 'trial_cooldown_status', 'updated_at')
    actions = ['reset_trial_access']

    @admin.display(description='Пробный доступ')
    def trial_cooldown_status(self, obj):
        if not obj.last_trial_at:
            return 'Не использовался'
        available_at = trial_available_at(obj)
        if available_at:
            local = timezone.localtime(available_at)
            return format_html('Ожидание до {} МСК', local.strftime('%d.%m.%Y %H:%M'))
        return 'Можно запросить снова'

    @admin.display(description='Чат')
    def chat_history_link(self, obj):
        if obj.username:
            url = (
                reverse('admin:ghost_note_ghosttelegrambotmessage_changelist')
                + f'?username={obj.username}'
            )
        else:
            url = (
                reverse('admin:ghost_note_ghosttelegrambotmessage_changelist')
                + f'?telegram_user_id={obj.telegram_user_id}'
            )
        return format_html('<a href="{}">Открыть чат</a>', url)

    @admin.display(description='Сброс')
    def reset_trial_action(self, obj):
        url = reverse('admin:ghost_note_ghosttelegramcontact_reset_trial', args=[obj.pk])
        disabled = not obj.last_trial_at
        return format_html(
            '<button type="button" class="btn btn-sm btn-outline-warning ghost-trial-reset-btn" '
            'data-url="{}"{}>Сбросить</button>',
            url,
            format_html(' disabled') if disabled else '',
        )

    @admin.action(description='Сбросить использование пробного доступа')
    def reset_trial_access(self, request, queryset):
        updated = queryset.update(last_trial_at=None)
        self.message_user(
            request,
            f'Пробный доступ сброшен для {updated} контакт(ов). Пользователь снова может запросить /trial.',
        )

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                '<int:object_id>/reset-trial/',
                self.admin_site.admin_view(self.reset_trial_view),
                name='ghost_note_ghosttelegramcontact_reset_trial',
            ),
        ]
        return custom_urls + urls

    def reset_trial_view(self, request, object_id):
        if request.method != 'POST':
            return JsonResponse({'ok': False, 'error': 'method_not_allowed'}, status=405)
        if not self.has_change_permission(request):
            return JsonResponse({'ok': False, 'error': 'forbidden'}, status=403)

        contact = get_object_or_404(GhostTelegramContact, pk=object_id)
        contact.last_trial_at = None
        contact.save(update_fields=['last_trial_at'])
        return JsonResponse({
            'ok': True,
            'last_trial_at': '—',
            'trial_status': 'Не использовался',
        })

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return True


@admin.register(GhostTelegramBotMessage)
class GhostTelegramBotMessageAdmin(admin.ModelAdmin):
    change_list_template = 'admin/ghost_note/ghosttelegrambotmessage/change_list.html'
    list_display = (
        'created_at_msk',
        'direction_display',
        'username_display',
        'telegram_user_id',
        'text_preview',
    )
    list_filter = ('direction', 'message_kind', 'created_at')
    search_fields = ('username', 'telegram_user_id', 'first_name', 'text')
    readonly_fields = (
        'telegram_user_id',
        'username',
        'first_name',
        'direction',
        'message_kind',
        'text',
        'created_at_msk',
    )
    ordering = ('created_at',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        chat_id = (request.GET.get('telegram_user_id') or '').strip()
        username = (request.GET.get('username') or '').strip().lstrip('@')
        if chat_id:
            qs = qs.filter(telegram_user_id=chat_id)
        elif username:
            qs = qs.filter(username__iexact=username)
        return qs

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        chat_id = (request.GET.get('telegram_user_id') or '').strip()
        username = (request.GET.get('username') or '').strip().lstrip('@')
        extra_context['ghost_chat_mode'] = bool(chat_id or username)
        if username:
            extra_context['ghost_chat_title'] = f'@{username}'
        elif chat_id:
            extra_context['ghost_chat_title'] = f'chat_id {chat_id}'
        if extra_context['ghost_chat_mode']:
            resolved_chat_id = resolve_chat_id(username=username, telegram_user_id=chat_id)
            extra_context['ghost_chat_telegram_user_id'] = resolved_chat_id or chat_id
            extra_context['ghost_chat_username'] = username
            extra_context['ghost_chat_send_url'] = reverse(
                'admin:ghost_note_ghosttelegrambotmessage_send_message',
            )
            messages = list(self.get_queryset(request).order_by('created_at'))
            extra_context['ghost_chat_messages'] = messages
            if messages:
                extra_context['ghost_chat_html'] = mark_safe(
                    ''.join(format_chat_message_html(message) for message in messages)
                )
            else:
                extra_context['ghost_chat_html'] = 'Сообщений пока нет.'
        return super().changelist_view(request, extra_context=extra_context)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                'send-message/',
                self.admin_site.admin_view(self.send_message_view),
                name='ghost_note_ghosttelegrambotmessage_send_message',
            ),
        ]
        return custom_urls + urls

    def send_message_view(self, request):
        if request.method != 'POST':
            return JsonResponse({'ok': False, 'error': 'method_not_allowed'}, status=405)
        if not self.has_view_permission(request):
            return JsonResponse({'ok': False, 'error': 'forbidden'}, status=403)
        if not is_ghost_bot_configured():
            return JsonResponse({'ok': False, 'error': 'bot_not_configured'}, status=503)

        text = (request.POST.get('text') or '').strip()
        if not text:
            return JsonResponse({'ok': False, 'error': 'empty_text'}, status=400)
        if len(text) > 4096:
            return JsonResponse({'ok': False, 'error': 'too_long'}, status=400)

        username = (request.POST.get('username') or '').strip().lstrip('@')
        telegram_user_id = (request.POST.get('telegram_user_id') or '').strip()
        chat_id = resolve_chat_id(username=username, telegram_user_id=telegram_user_id)
        if not chat_id:
            return JsonResponse({'ok': False, 'error': 'chat_not_found'}, status=404)

        try:
            send_bot_message(chat_id=chat_id, text=text, parse_mode=None)
        except Exception as exc:
            logger.exception('Admin chat send failed for chat_id=%s', chat_id)
            return JsonResponse({'ok': False, 'error': str(exc)[:200]}, status=502)

        message = (
            GhostTelegramBotMessage.objects.filter(
                telegram_user_id=chat_id,
                direction=GhostTelegramBotMessage.Direction.OUT,
            )
            .order_by('-created_at')
            .first()
        )
        return JsonResponse({
            'ok': True,
            'message_html': str(format_chat_message_html(message)) if message else '',
        })

    @admin.display(description='Когда', ordering='created_at')
    def created_at_msk(self, obj):
        return format_token_datetime(obj.created_at)

    @admin.display(description='↔', ordering='direction')
    def direction_display(self, obj):
        if obj.direction == GhostTelegramBotMessage.Direction.OUT:
            return format_html('<span title="Исходящее">→</span>')
        return format_html('<span title="Входящее">←</span>')

    @admin.display(description='Username', ordering='username')
    def username_display(self, obj):
        if obj.username:
            url = (
                reverse('admin:ghost_note_ghosttelegrambotmessage_changelist')
                + f'?username={obj.username}'
            )
            return format_html('<a href="{}">@{}</a>', url, obj.username)
        return '—'

    @admin.display(description='Текст')
    def text_preview(self, obj):
        preview = strip_html(obj.text)
        if len(preview) > 120:
            preview = preview[:117] + '…'
        return preview
