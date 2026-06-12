import argparse
import sys
from pathlib import Path

WELCOME_DIR = Path(__file__).resolve().parent / 'welcome'
sys.path.insert(0, str(WELCOME_DIR))

from ai_chat.yandex_client import create_response  # noqa: E402


def ask(prompt, image_path=None, instructions='', no_reasoning=False):
    image_bytes = None
    mime_type = None

    if image_path is not None:
        if not image_path.is_file():
            raise FileNotFoundError(f'Image not found: {image_path}')
        with open(image_path, 'rb') as image_file:
            raw = image_file.read()
        suffix = image_path.suffix.lower()
        mime_type = {
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.webp': 'image/webp',
            '.gif': 'image/gif',
            '.bmp': 'image/bmp',
        }.get(suffix, 'image/jpeg')
        from ai_chat.yandex_client import _convert_image_bytes

        image_bytes, mime_type = _convert_image_bytes(raw, mime_type)

    reply, _ = create_response(
        prompt=prompt,
        image_bytes=image_bytes,
        mime_type=mime_type,
        instructions=instructions,
        reasoning_effort='none' if no_reasoning else None,
    )
    return reply


def parse_args():
    parser = argparse.ArgumentParser(description='Yandex AI Studio helper')
    parser.add_argument('prompt', nargs='?', help='Question or task for the model')
    parser.add_argument('-i', '--image', type=Path, help='Path to image file')
    parser.add_argument('--instructions', default='', help='System instructions')
    parser.add_argument(
        '--no-reasoning',
        action='store_true',
        help='Disable Qwen reasoning mode',
    )
    return parser.parse_args()


def main():
    args = parse_args()
    prompt = args.prompt

    if not prompt:
        prompt = input('Prompt: ').strip()
    if not prompt:
        print('Prompt is required', file=sys.stderr)
        sys.exit(1)

    try:
        print(ask(prompt, image_path=args.image, instructions=args.instructions, no_reasoning=args.no_reasoning))
    except Exception as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
