from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('main.urls')),
    path('', include('ai_chat.urls')),
    path('', include('webhook_tg.urls')),
    path('ghost/', include('ghost_note.urls')),
] + static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
