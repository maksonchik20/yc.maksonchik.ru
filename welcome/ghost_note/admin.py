from django import forms
from django.contrib import admin
from django.contrib.admin import widgets as admin_widgets
from django.core.exceptions import ValidationError
from django.utils import timezone

from .auth import format_token_datetime
from .models import GhostAccessToken, GhostSession, GhostTextMessage

MSK_DATETIME_INPUT_FORMATS = [
    '%d.%m.%Y %H:%M',
    '%d.%m.%Y %H:%M:%S',
    '%Y-%m-%d %H:%M:%S',
    '%Y-%m-%d %H:%M',
]


class GhostAccessTokenAdminForm(forms.ModelForm):
    starts_at = forms.SplitDateTimeField(
        label='Действителен с',
        widget=admin_widgets.AdminSplitDateTime(),
        input_formats=MSK_DATETIME_INPUT_FORMATS,
    )
    expires_at = forms.SplitDateTimeField(
        label='Действителен до',
        widget=admin_widgets.AdminSplitDateTime(),
        input_formats=MSK_DATETIME_INPUT_FORMATS,
    )

    class Meta:
        model = GhostAccessToken
        fields = '__all__'

    def clean(self):
        cleaned = super().clean()
        allow_local = cleaned.get('allow_local')
        allow_remote = cleaned.get('allow_remote')
        if allow_local is False and allow_remote is False:
            raise ValidationError('Выберите хотя бы один вариант использования: локальный или удалённый.')
        starts_at = cleaned.get('starts_at')
        expires_at = cleaned.get('expires_at')
        if starts_at and expires_at and starts_at >= expires_at:
            raise ValidationError('Время начала должно быть раньше времени окончания.')
        return cleaned


@admin.register(GhostAccessToken)
class GhostAccessTokenAdmin(admin.ModelAdmin):
    form = GhostAccessTokenAdminForm
    list_display = (
        'token_preview', 'label', 'allow_local', 'allow_remote',
        'starts_at_msk', 'expires_at_msk', 'is_active',
        'last_used_at_msk', 'created_at_msk',
    )
    list_filter = ('is_active', 'allow_local', 'allow_remote')
    search_fields = ('token', 'label')
    readonly_fields = ('token', 'created_at_msk', 'last_used_at_msk', 'viewer_link')
    fieldsets = (
        (None, {
            'fields': (
                'label', 'token', 'starts_at', 'expires_at', 'is_active',
                'allow_local', 'allow_remote', 'viewer_link',
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
        return f'/ghost/viewer/?token={quote(obj.token, safe="")}'

    viewer_link.short_description = 'Ссылка viewer'

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        now = timezone.now()
        initial.setdefault('starts_at', now)
        initial.setdefault('expires_at', now + timezone.timedelta(days=7))
        return initial


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
