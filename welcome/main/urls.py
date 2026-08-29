from django.urls import path

from ghost_note import purchase_views

from . import views
from .site_info import INDEXNOW_KEY

urlpatterns = [
    path('', views.index, name='index'),
    path('proktoring/', views.proktoring, name='proktoring'),
    path('oferta/', views.oferta, name='oferta'),
    path('privacy/', views.privacy, name='privacy'),
    path('sitemap.xml', views.sitemap_xml, name='sitemap_xml'),
    path('robots.txt', views.robots_txt, name='robots_txt'),
    path('favicon.svg', views.favicon_svg, name='favicon_svg'),
    path(f'{INDEXNOW_KEY}.txt', views.indexnow_key_file, name='indexnow_key_file'),
    path('buy/', purchase_views.buy, name='ghost_buy'),
    path('buy/success/<uuid:public_id>/', purchase_views.buy_success, name='ghost_buy_success'),
    path('buy/status/<uuid:public_id>/', purchase_views.buy_status, name='ghost_buy_status'),
    path('send/', views.send_message, name='send'),
    path('get/', views.get_messages, name='get'),
    path('send_tg/', views.send_tg, name='send_tg'),
]
