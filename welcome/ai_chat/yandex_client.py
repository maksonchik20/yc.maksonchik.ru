import base64
import io

import openai
from env import YANDEX_CLOUD_API_KEY, YANDEX_CLOUD_FOLDER, YANDEX_CLOUD_MODEL

client = openai.OpenAI(
    api_key=YANDEX_CLOUD_API_KEY,
    base_url='https://ai.api.cloud.yandex.net/v1',
    project=YANDEX_CLOUD_FOLDER,
)

DIRECT_API_MIME_TYPES = {
    'image/jpeg',
    'image/jpg',
    'image/png',
}


def _convert_image_bytes(image_bytes, mime_type):
    if mime_type in DIRECT_API_MIME_TYPES:
        return image_bytes, mime_type

    try:
        from PIL import Image
    except ImportError as exc:
        raise ValueError(
            'Формат изображения не поддерживается API. '
            'Отправьте jpg/png или установите pillow на сервере.'
        ) from exc

    with Image.open(io.BytesIO(image_bytes)) as image:
        if image.mode not in ('RGB', 'L'):
            image = image.convert('RGB')
        buffer = io.BytesIO()
        image.save(buffer, format='JPEG', quality=90)
        return buffer.getvalue(), 'image/jpeg'


def parse_image_base64(image_base64):
    if not image_base64:
        return None, None

    raw = image_base64.strip()
    mime_type = 'image/jpeg'

    if raw.startswith('data:'):
        header, _, payload = raw.partition(',')
        if not payload:
            raise ValueError('Invalid data URI in image_base64')
        if ';base64' in header:
            mime_type = header[5:].split(';', 1)[0] or mime_type
        raw = payload

    try:
        image_bytes = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise ValueError('image_base64 must be valid base64') from exc

    return _convert_image_bytes(image_bytes, mime_type)


def build_input(prompt, image_bytes=None, mime_type=None):
    if not image_bytes:
        return prompt

    encoded = base64.b64encode(image_bytes).decode('ascii')
    data_uri = f'data:{mime_type};base64,{encoded}'
    return [
        {
            'role': 'user',
            'content': [
                {'type': 'input_text', 'text': prompt},
                {'type': 'input_image', 'image_url': data_uri},
            ],
        }
    ]


def extract_response_text(response):
    if getattr(response, 'error', None):
        raise RuntimeError(f'API error: {response.error}')

    if getattr(response, 'status', None) == 'failed':
        raise RuntimeError(f'API request failed: {response.error or response.incomplete_details}')

    text = (response.output_text or '').strip()
    if text:
        return text

    parts = []
    for item in response.output or []:
        if getattr(item, 'type', None) != 'message':
            continue
        for content in getattr(item, 'content', []) or []:
            if getattr(content, 'type', None) == 'output_text':
                chunk = (getattr(content, 'text', '') or '').strip()
                if chunk:
                    parts.append(chunk)

    if parts:
        return '\n'.join(parts)

    raise RuntimeError(f'Empty model response (status={getattr(response, "status", None)})')


def create_response(
    prompt,
    previous_response_id=None,
    image_bytes=None,
    mime_type=None,
    instructions='',
    temperature=0.3,
    max_output_tokens=10000,
    reasoning_effort=None,
    model=None,
):
    if not prompt.strip():
        raise ValueError('Prompt is empty')

    model_name = model or YANDEX_CLOUD_MODEL
    request_kwargs = {
        'model': f'gpt://{YANDEX_CLOUD_FOLDER}/{model_name}',
        'temperature': temperature,
        'instructions': instructions,
        'input': build_input(prompt, image_bytes, mime_type),
        'max_output_tokens': max_output_tokens,
    }

    if previous_response_id:
        request_kwargs['previous_response_id'] = previous_response_id

    if reasoning_effort is not None:
        request_kwargs['reasoning'] = {'effort': reasoning_effort}

    response = client.responses.create(**request_kwargs)
    return extract_response_text(response), response.id
