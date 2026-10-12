from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from .auth import format_token_datetime
from .models import (GhostAccessToken, GhostAIRequest, GhostSecuritySession,
                     GhostTextMessage, GhostTokenDevice, GhostTokenScreenshot)


def date(value):
    return format_token_datetime(value) if value else '—'


def listing(model, token_id, lookup='access_token__id__exact'):
    return reverse('admin:ghost_note_{}_changelist'.format(model)) + '?{}={}'.format(lookup, token_id)


def table(headers, rows):
    return format_html('<div style="overflow:auto;max-height:480px"><table><thead><tr>{}</tr></thead><tbody>{}</tbody></table></div>',
        format_html_join('', '<th>{}</th>', ((header,) for header in headers)),
        format_html_join('', '<tr>{}</tr>', ((format_html_join('', '<td style="white-space:pre-wrap;max-width:600px">{}</td>',
            ((cell,) for cell in row)),) for row in rows)))


class TokenActivityAdminMixin:
    def get_urls(self):
        return [path('<int:token_id>/archive/<int:image_id>/', self.admin_site.admin_view(self.archive_image),
                     name='ghost_note_token_archive_image')] + super().get_urls()

    def archive_image(self, request, token_id, image_id):
        token = get_object_or_404(GhostAccessToken, pk=token_id)
        if not self.has_view_permission(request, token):
            raise PermissionDenied
        image = get_object_or_404(GhostTokenScreenshot, pk=image_id, access_token_id=token_id)
        response = HttpResponse(bytes(image.image), content_type='image/jpeg')
        response['Cache-Control'] = 'private, no-store'
        response['X-Content-Type-Options'] = 'nosniff'
        response['Content-Security-Policy'] = "default-src 'none'"
        return response

    @admin.display(description='Устройства')
    def devices_section(self, obj):
        if not obj or not obj.pk:
            return 'Появятся после подключения защищённой программы.'
        rows = []
        for device in obj.devices.order_by('-last_seen_at')[:50]:
            link = format_html('<a href="{}">{}</a>', reverse('admin:ghost_note_ghosttokendevice_change', args=[device.pk]), str(device))
            rows.append((link, 'Заблокировано' if device.blocked else 'Разрешено', date(device.first_seen_at), date(device.last_seen_at)))
        return format_html('<p>Старые .exe не сообщают ключ устройства и продолжают работать без этих ограничений.</p>{}<p><a href="{}">Все устройства / блокировка / смена компьютера</a></p>',
            table(['Устройство', 'Статус', 'Подключено', 'Активность'], rows), listing('ghosttokendevice', obj.pk))

    @admin.display(description='Сессии')
    def sessions_section(self, obj):
        if not obj or not obj.pk:
            return '—'
        now = timezone.now()
        rows = []
        for session in obj.security_sessions.order_by('-last_seen_at')[:50]:
            link = format_html('<a href="{}">Сессия #{}</a>', reverse('admin:ghost_note_ghostsecuritysession_change', args=[session.pk]), session.pk)
            status = 'Завершена' if session.revoked_at else ('Аренда действует' if session.lease_expires_at > now else 'Аренда истекла')
            rows.append((link, session.device_key_hash[:12], session.get_mode_display() or 'Не указан', status,
                         date(session.last_seen_at), date(session.lease_expires_at), 'Да' if session.recording_consent_at else 'Нет'))
        legacy = obj.sessions.filter(security_v2__isnull=True).count()
        return format_html('{}<p>Сессий старого клиента: {}. Точную активность отдельных компьютеров старый протокол не передаёт.</p><p><a href="{}">Управление защищёнными сессиями</a></p>',
            table(['Сессия', 'Устройство', 'Режим', 'Статус', 'Активность', 'Аренда до', 'Архив разрешён'], rows), legacy,
            listing('ghostsecuritysession', obj.pk))

    @admin.display(description='Сообщения')
    def messages_section(self, obj):
        if not obj or not obj.pk:
            return '—'
        items = GhostTextMessage.objects.filter(session__access_token=obj).order_by('-created_at', '-pk')[:50]
        rows = [(date(item.created_at), item.get_source_display(), item.text[:2000], 'Да' if item.delivered else 'Нет') for item in items]
        return format_html('<p>Последние 50 сообщений. Локальные сообщения появятся после согласия в новой программе.</p>{}<p><a href="{}">Все сообщения</a></p>',
            table(['Отправлено', 'Источник', 'Текст', 'Доставлено'], rows), listing('ghosttextmessage', obj.pk, 'session__access_token__id__exact'))

    @admin.display(description='Снимки экрана')
    def screenshots_section(self, obj):
        if not obj or not obj.pk:
            return '—'
        images = obj.archived_screenshots.defer('image').order_by('-created_at')[:24]
        thumbnails = format_html_join('', '<figure style="margin:8px;display:inline-block"><a href="{}" target="_blank" rel="noopener"><img src="{}" loading="lazy" width="240" alt="Снимок экрана"></a><figcaption>{}</figcaption></figure>',
            ((reverse('admin:ghost_note_token_archive_image', args=[obj.pk, image.pk]),
              reverse('admin:ghost_note_token_archive_image', args=[obj.pk, image.pk]), date(image.created_at)) for image in images))
        return format_html('<p>Раз в 5 минут, только после согласия пользователя. Хранение: {} дней. Последние 24 снимка.</p>{}<p><a href="{}">Все снимки</a></p>',
            max(1, min(30, obj.screenshot_retention_days)), thumbnails or 'Снимков пока нет.', listing('ghosttokenscreenshot', obj.pk))

    @admin.display(description='История AI')
    def ai_section(self, obj):
        if not obj or not obj.pk:
            return '—'
        items = GhostAIRequest.objects.filter(thread__access_token=obj).defer('image').order_by('-created_at')[:50]
        rows = [(date(item.created_at), item.get_status_display(), item.answer[:2000] or item.error_code) for item in items]
        return format_html('<p>Последний запрос: {}. Интервал не меньше 30 секунд на весь токен. Суточный лимит: {}.</p>{}<p><a href="{}">Все AI-запросы</a></p>',
            date(obj.last_ai_used_at), obj.ai_daily_request_limit or 'не установлен',
            table(['Запрос', 'Статус', 'Ответ / ошибка'], rows), listing('ghostairequest', obj.pk, 'thread__access_token__id__exact'))


@admin.register(GhostTokenDevice)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'access_token', 'blocked', 'first_seen_at', 'last_seen_at')
    list_filter = ('blocked',)
    search_fields = ('label', 'device_key_hash', 'access_token__token')
    fields = ('access_token', 'device_key_hash', 'label', 'blocked', 'first_seen_at', 'last_seen_at')
    readonly_fields = ('access_token', 'device_key_hash', 'first_seen_at', 'last_seen_at')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        # Blocking also revokes leases so an unblock cannot revive an old session.
        super().save_model(request, obj, form, change)
        if obj.blocked:
            GhostSecuritySession.objects.filter(access_token=obj.access_token, device_key_hash=obj.device_key_hash,
                revoked_at__isnull=True).update(revoked_at=timezone.now(), recording_consent_at=None)


@admin.register(GhostSecuritySession)
class SecuritySessionAdmin(admin.ModelAdmin):
    list_display = ('id', 'access_token', 'device_key_hash', 'mode', 'last_seen_at', 'lease_expires_at', 'revoked_at')
    list_filter = ('mode', 'revoked_at')
    search_fields = ('access_token__token', 'device_key_hash')
    fields = ('access_token', 'device_key_hash', 'mode', 'created_at', 'last_seen_at', 'lease_expires_at', 'revoked_at', 'recording_consent_at')
    readonly_fields = fields
    actions = ('revoke_sessions',)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description='Завершить выбранные сессии')
    def revoke_sessions(self, request, queryset):
        count = queryset.filter(revoked_at__isnull=True).update(revoked_at=timezone.now(), recording_consent_at=None)
        self.message_user(request, 'Завершено сессий: {}. Локальный клиент остановится после следующей проверки сервера.'.format(count), messages.SUCCESS)


@admin.register(GhostAIRequest)
class AIRequestAdmin(admin.ModelAdmin):
    list_display = ('id', 'thread', 'status', 'created_at', 'completed_at', 'error_code')
    list_filter = ('status', 'thread__access_token')
    search_fields = ('thread__access_token__token', 'answer')
    fields = ('thread', 'request_id', 'conversation_id', 'status', 'created_at', 'completed_at', 'error_code', 'answer')
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(GhostTokenScreenshot)
class ScreenshotAdmin(admin.ModelAdmin):
    list_display = ('id', 'access_token', 'created_at', 'preview')
    list_per_page = 20
    list_filter = ('created_at',)
    search_fields = ('access_token__token', 'access_token__user__name')
    fields = ('access_token', 'session', 'created_at', 'preview')
    readonly_fields = fields

    def get_queryset(self, request):
        return super().get_queryset(request).defer('image').select_related('access_token')

    def has_add_permission(self, request):
        return False

    @admin.display(description='Снимок')
    def preview(self, obj):
        url = reverse('admin:ghost_note_token_archive_image', args=[obj.access_token_id, obj.pk])
        return format_html('<a href="{}" target="_blank" rel="noopener"><img loading="lazy" src="{}" width="240" alt="Снимок"></a>', url, url)
