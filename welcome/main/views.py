from pathlib import Path

import json

import requests
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from env import token, vlad, my, isaev, roma
from .models import Messages

names = {'vlad': vlad, 'my': my, 'isaev': isaev, 'roma': roma}
api_tg_url = f'https://api.telegram.org/bot{token}'


def send_bot_tg(chat_id, text):
    requests.post(
        f'{api_tg_url}/sendMessage',
        json={'chat_id': chat_id, 'text': text},
        timeout=30,
    )


from django.shortcuts import render


def index(request):
    return render(request, 'main/index.html')


def proktoring(request):
    return render(request, 'main/proktoring.html')


def oferta(request):
    return render(request, 'main/oferta.html')


def privacy(request):
    return render(request, 'main/privacy.html')


def _read_root_file(name: str, content_type: str) -> HttpResponse:
    path = Path(__file__).resolve().parent.parent / name
    return HttpResponse(path.read_text(encoding="utf-8"), content_type=content_type)


def sitemap_xml(request):
    return _read_root_file("sitemap.xml", "application/xml; charset=UTF-8")


def robots_txt(request):
    return _read_root_file("robots.txt", "text/plain; charset=UTF-8")


def favicon_svg(request):
    return _read_root_file("favicon.svg", "image/svg+xml")


def indexnow_key_file(request):
    """Ключ IndexNow в корне сайта (UTF-8, без HTML)."""
    from .site_info import INDEXNOW_KEY

    return _read_root_file(f"{INDEXNOW_KEY}.txt", "text/plain; charset=UTF-8")


@csrf_exempt
def send_message(request):
    if request.method != 'POST':
        return HttpResponse('Send POST request')
    body = dict(json.loads(request.body))
    if body.get('message', None) is not None and isinstance(body['message'], str):
        message = body['message']
        Messages.objects.create(text=message)
        return HttpResponse('Your message delivered')
    return HttpResponse("Pass the string data type in the 'message' field")


@csrf_exempt
def get_messages(request):
    if request.method != 'POST':
        return HttpResponse('Send POST request')
    messages = list(Messages.objects.all().values_list('text'))
    for i in range(len(messages)):
        messages[i] = messages[i][0]
    print(messages)
    return JsonResponse({'Messages': messages})


@csrf_exempt
def send_tg(request):
    if request.method != 'POST':
        return HttpResponse('Send POST request')
    body = dict(json.loads(request.body))
    if body.get('message', None) is None:
        return HttpResponse("Pass the string data type in the 'message' field")
    if not isinstance(body['message'], str):
        return HttpResponse("Pass the string data type in the 'message' field")
    if body.get('name', None) is None:
        return HttpResponse("Pass the string data type in the 'name' field")
    if not isinstance(body['name'], str):
        return HttpResponse("Pass the string data type in the 'message' field")
    message = body['message']
    name = body['name']
    if names.get(name, None) is None:
        return HttpResponse('name not found')
    try:
        send_bot_tg(names[name], message)
        return HttpResponse('Your message delivered')
    except Exception as ex:
        return HttpResponse(f'Error: {ex}')
