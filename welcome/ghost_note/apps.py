from django.apps import AppConfig


class GhostNoteConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'ghost_note'
    verbose_name = 'Ghost Note'

    def ready(self):
        from . import signals  # noqa: F401
