"""Отправка URL в Яндекс IndexNow.

Примеры:
  python manage.py submit_indexnow
  python manage.py submit_indexnow https://yc.maksonchik.ru/buy/
"""

from __future__ import annotations

import json

import requests
from django.core.management.base import BaseCommand

from main.site_info import INDEXNOW_ENDPOINT, INDEXNOW_HOST, INDEXNOW_KEY


DEFAULT_URLS = [
    f'https://{INDEXNOW_HOST}/',
    f'https://{INDEXNOW_HOST}/buy/',
    f'https://{INDEXNOW_HOST}/proktoring/',
    f'https://{INDEXNOW_HOST}/oferta/',
    f'https://{INDEXNOW_HOST}/privacy/',
]


class Command(BaseCommand):
    help = 'Submit site URLs to Yandex IndexNow API.'

    def add_arguments(self, parser):
        parser.add_argument(
            'urls',
            nargs='*',
            help='URLs to submit (default: public pages from sitemap)',
        )

    def handle(self, *args, **options):
        urls = options['urls'] or DEFAULT_URLS
        payload = {
            'host': INDEXNOW_HOST,
            'key': INDEXNOW_KEY,
            'urlList': list(urls),
        }
        self.stdout.write(f'POST {INDEXNOW_ENDPOINT}')
        self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
        response = requests.post(
            INDEXNOW_ENDPOINT,
            json=payload,
            headers={'Content-Type': 'application/json; charset=utf-8'},
            timeout=30,
        )
        self.stdout.write(f'HTTP {response.status_code}')
        body = (response.text or '').strip()
        if body:
            self.stdout.write(body[:1000])
        if response.status_code in (200, 202):
            self.stdout.write(self.style.SUCCESS('IndexNow accepted'))
        else:
            self.stderr.write(self.style.ERROR('IndexNow failed'))
