from django.urls import path

from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('send/', views.send_message, name='send'),
    path('get/', views.get_messages, name='get'),
    path('send_tg/', views.send_tg, name='send_tg'),
]
