from django.contrib import admin

from .models import GhostSession, GhostTextMessage


@admin.register(GhostSession)
class GhostSessionAdmin(admin.ModelAdmin):
    list_display = ('session_id', 'screenshot_updated_at', 'created_at')
    readonly_fields = ('session_id', 'created_at', 'updated_at', 'screenshot_updated_at')
    search_fields = ('session_id',)


@admin.register(GhostTextMessage)
class GhostTextMessageAdmin(admin.ModelAdmin):
    list_display = ('session', 'text_preview', 'delivered', 'created_at')
    list_filter = ('delivered',)
    search_fields = ('text', 'session__session_id')

    def text_preview(self, obj):
        return obj.text[:80] + ('…' if len(obj.text) > 80 else '')

    text_preview.short_description = 'Text'
