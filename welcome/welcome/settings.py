from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = '6dcvhyz21q@rs%6+h8##v#zpryygs@-+u13hp3%3%9n--&!$f0'

DEBUG = False

ALLOWED_HOSTS = ['yc.maksonchik.ru', 'localhost', '127.0.0.1']

INSTALLED_APPS = [
    'jazzmin',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'webhook_tg.apps.WebhookTgConfig',
    'main.apps.MainConfig',
    'ai_chat.apps.AiChatConfig',
    'ghost_note.apps.GhostNoteConfig',
]

DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'welcome.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'main' / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'main.context_processors.site_info',
            ],
        },
    },
]

WSGI_APPLICATION = 'welcome.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': 'django',
        'USER': 'django',
        'HOST': 'localhost',
        'PASSWORD': 'f7XKuq7sdGP3o',
        'OPTIONS': {
            'charset': 'utf8mb4',
        },
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

LANGUAGE_CODE = 'ru-ru'

TIME_ZONE = 'Europe/Moscow'

USE_I18N = True

USE_TZ = True

DATE_FORMAT = 'd.m.Y'
TIME_FORMAT = 'H:i'
DATETIME_FORMAT = 'd.m.Y H:i'
SHORT_DATETIME_FORMAT = 'd.m.Y H:i'
DATE_INPUT_FORMATS = ['%Y-%m-%d', '%d.%m.%Y']
TIME_INPUT_FORMATS = ['%H:%M:%S', '%H:%M']

STATIC_URL = '/static/'
STATIC_ROOT = '/usr/share/django-projects/welcome/static/'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

JAZZMIN_SETTINGS = {
    'site_title': 'Ghost Note Admin',
    'site_header': 'Ghost Note',
    'site_brand': 'Ghost Note',
    'welcome_sign': 'Панель управления Ghost Note',
    'copyright': 'Ghost Note',
    'search_model': ['ghost_note.GhostUser', 'ghost_note.GhostAccessToken'],
    'topmenu_links': [
        {'name': 'Пользователи', 'url': 'admin:ghost_note_ghostuser_changelist'},
        {'name': 'Токены', 'url': 'admin:ghost_note_ghostaccesstoken_changelist'},
        {'name': 'Оплаты', 'url': 'admin:ghost_note_ghostrealpayment_changelist'},
        {'name': 'Выплаты', 'url': 'admin:ghost_note_ghostreferralpayout_changelist'},
    ],
    'order_with_respect_to': ['ghost_note'],
    'icons': {
        'ghost_note.GhostUser': 'fas fa-user',
        'ghost_note.GhostAccessToken': 'fas fa-key',
        'ghost_note.GhostRealPayment': 'fas fa-ruble-sign',
        'ghost_note.GhostReferralPayout': 'fas fa-money-check-alt',
        'ghost_note.GhostReferralCommission': 'fas fa-hand-holding-usd',
        'ghost_note.GhostSession': 'fas fa-desktop',
    },
    'custom_css': 'ghost_note/admin/referrals.css',
}

try:
    from env import YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY
except ImportError:
    YOOKASSA_SHOP_ID = ''
    YOOKASSA_SECRET_KEY = ''

try:
    from env import GHOST_NOTE_BOT_TOKEN, GHOST_NOTE_BOT_USERNAME, OWNER_CHAT_ID
    from env import GHOST_NOTE_DOWNLOAD_URL, GHOST_NOTE_INSTALLER_FILE_ID
except ImportError:
    GHOST_NOTE_BOT_TOKEN = ''
    GHOST_NOTE_BOT_USERNAME = ''
    OWNER_CHAT_ID = ''
    GHOST_NOTE_DOWNLOAD_URL = ''
    GHOST_NOTE_INSTALLER_FILE_ID = ''

GHOST_NOTE_ADMIN_CHAT_ID = OWNER_CHAT_ID

try:
    from env import EMAIL_HOST_USER, EMAIL_HOST_PASSWORD
except ImportError:
    EMAIL_HOST_USER = ''
    EMAIL_HOST_PASSWORD = ''

EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = 'smtp.mail.ru'
EMAIL_PORT = 465
EMAIL_USE_SSL = True
EMAIL_USE_TLS = False
DEFAULT_FROM_EMAIL = f'Ghost Note <{EMAIL_HOST_USER or "yalexer75@mail.ru"}>'
SERVER_EMAIL = DEFAULT_FROM_EMAIL
