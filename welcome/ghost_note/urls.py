from django.urls import path

from . import views

urlpatterns = [
    path('auth/', views.validate_token, name='ghost_validate_token'),
    path('viewer/<uuid:session_id>/', views.viewer, name='ghost_viewer'),
    path('screenshot/', views.upload_screenshot, name='ghost_upload_screenshot'),
    path('screenshot/get/', views.get_screenshot, name='ghost_get_screenshot'),
    path('text/', views.post_text, name='ghost_post_text'),
    path('text/poll/', views.poll_text, name='ghost_poll_text'),
    path('session/', views.register_session, name='ghost_register_session'),
]
