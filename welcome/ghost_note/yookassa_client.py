import base64
import ipaddress
import uuid
from decimal import Decimal

import requests
from django.conf import settings

YOOKASSA_API_URL = 'https://api.yookassa.ru/v3/payments'

YOOKASSA_WEBHOOK_IPS = [
    ipaddress.ip_network('185.71.76.0/27'),
    ipaddress.ip_network('185.71.77.0/27'),
    ipaddress.ip_network('77.75.153.0/25'),
    ipaddress.ip_network('77.75.154.128/25'),
    ipaddress.ip_network('2a02:5180::/32'),
]
YOOKASSA_WEBHOOK_IPS.extend([
    ipaddress.ip_network('77.75.156.11/32'),
    ipaddress.ip_network('77.75.156.35/32'),
])

PRICE_LOCAL = Decimal('2500.00')
PRICE_REMOTE = Decimal('3000.00')
MAX_DURATION_MINUTES = 180


class YooKassaError(Exception):
    pass


def _credentials():
    shop_id = str(settings.YOOKASSA_SHOP_ID or '').strip()
    secret_key = str(settings.YOOKASSA_SECRET_KEY or '').strip()
    if not shop_id or not secret_key:
        raise YooKassaError('ЮKassa не настроена: укажите YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY')
    if not (secret_key.startswith('test_') or secret_key.startswith('live_')):
        raise YooKassaError(
            'Секретный ключ должен начинаться с test_ или live_. Проверьте ключ в личном кабинете ЮKassa.'
        )
    return shop_id, secret_key


def _auth_header():
    shop_id, secret_key = _credentials()
    token = base64.b64encode(f'{shop_id}:{secret_key}'.encode('utf-8')).decode('ascii')
    return {'Authorization': f'Basic {token}'}


def is_yookassa_configured():
    try:
        _credentials()
        return True
    except YooKassaError:
        return False


def price_for_access(access_type):
    if access_type == 'local':
        return PRICE_LOCAL
    if access_type == 'remote':
        return PRICE_REMOTE
    raise ValueError(f'Unknown access type: {access_type}')


def _headers(idempotence_key=None):
    headers = {
        'Content-Type': 'application/json',
        **_auth_header(),
    }
    if idempotence_key:
        headers['Idempotence-Key'] = idempotence_key
    return headers


def _raise_for_response(response):
    if response.status_code in (200, 201):
        return
    if response.status_code == 401:
        raise YooKassaError(
            'ЮKassa отклонила авторизацию (401). Проверьте shopId и секретный ключ: '
            'они должны быть из одного магазина и одного режима (тест/боевой). '
            f'Ответ: {response.text}'
        )
    raise YooKassaError(f'ЮKassa HTTP {response.status_code}: {response.text}')


def create_payment(*, amount, description, return_url, metadata=None):
    payload = {
        'amount': {
            'value': f'{Decimal(amount):.2f}',
            'currency': 'RUB',
        },
        'capture': True,
        'confirmation': {
            'type': 'redirect',
            'return_url': return_url,
        },
        'description': description[:128],
        'metadata': metadata or {},
    }
    response = requests.post(
        YOOKASSA_API_URL,
        json=payload,
        headers=_headers(str(uuid.uuid4())),
        timeout=30,
    )
    _raise_for_response(response)
    data = response.json()
    confirmation = data.get('confirmation') or {}
    return {
        'id': data.get('id', ''),
        'status': data.get('status', ''),
        'confirmation_url': confirmation.get('confirmation_url', ''),
        'paid': data.get('paid', False),
        'raw': data,
    }


def get_payment(payment_id):
    response = requests.get(
        f'{YOOKASSA_API_URL}/{payment_id}',
        headers=_auth_header(),
        timeout=30,
    )
    _raise_for_response(response)
    return response.json()


def is_yookassa_webhook_ip(ip_str):
    if not ip_str:
        return False
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    for network in YOOKASSA_WEBHOOK_IPS:
        if ip in network:
            return True
    return False
