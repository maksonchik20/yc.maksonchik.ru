from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import GhostAccessToken, GhostSession, GhostTextMessage


class GhostAccessTokenAdminForm(forms.ModelForm):
    class Meta:
        model = GhostAccessToken
        fields = '__all__'

    def clean(self):
        cleaned = super().clean()
        allow_local = cleaned.get('allow_local')
        allow_remote = cleaned.get('allow_remote')
        if allow_local is False and allow_remote is False:
            raise ValidationError('Выберите хотя бы один вариант использования: локальный или удалённый.')
        return cleaned


@admin.register(GhostAccessToken)
class GhostAccessTokenAdmin(admin.ModelAdmin):
    form = GhostAccessTokenAdminForm
    list_display = (
        'token_preview', 'label', 'allow_local', 'allow_remote',
        'expires_at', 'is_active', 'last_used_at', 'created_at',
    )
    list_filter = ('is_active', 'allow_local', 'allow_remote')
    search_fields = ('token', 'label')
    readonly_fields = ('token', 'created_at', 'last_used_at', 'viewer_link')
    fieldsets = (
        (None, {
            'fields': (
                'label', 'token', 'expires_at', 'is_active',
                'allow_local', 'allow_remote', 'viewer_link',
            ),
        }),
        ('Служебное', {
            'fields': ('created_at', 'last_used_at'),
        }),
    )

    def token_preview(self, obj):
        return obj.token

    token_preview.short_description = 'Токен'

    def viewer_link(self, obj):
        from urllib.parse import quote

        if not obj.token:
            return '—'
        return f'/ghost/viewer/?token={quote(obj.token, safe="")}'

    viewer_link.short_description = 'Ссылка viewer'

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        initial.setdefault('expires_at', timezone.now() + timezone.timedelta(days=7))
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
