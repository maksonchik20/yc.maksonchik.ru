import argparse
import base64
import json
import re
import sys
from pathlib import Path

import requests

DEFAULT_URL = "https://yc.maksonchik.ru"


def format_error(response):
    try:
        data = response.json()
        if isinstance(data, dict) and data.get("error"):
            return data["error"]
        return json.dumps(data, ensure_ascii=False)
    except ValueError:
        text = response.text
        title = re.search(r"<title>(.*?)</title>", text, re.I | re.S)
        if title:
            return title.group(1).strip()
        snippet = text.strip().replace("\n", " ")
        return snippet[:300] or "non-JSON response"


def post(url, path, payload):
    response = requests.post(
        f"{url.rstrip('/')}/{path.lstrip('/')}",
        json=payload,
        timeout=120,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {format_error(response)}")

    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"Expected JSON, got: {format_error(response)}") from exc


def send_message(url, message, session_id=None, image_path=None, no_reasoning=False, instructions=""):
    payload = {
        "message": message,
        "no_reasoning": no_reasoning,
    }
    if session_id:
        payload["session_id"] = session_id
    if instructions:
        payload["instructions"] = instructions
    if image_path:
        image_bytes = Path(image_path).read_bytes()
        suffix = Path(image_path).suffix.lower()
        mime_type = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".bmp": "image/bmp",
        }.get(suffix, "image/jpeg")
        encoded = base64.b64encode(image_bytes).decode("ascii")
        payload["image_base64"] = f"data:{mime_type};base64,{encoded}"

    return post(url, "/ai/send/", payload)


def get_history(url, session_id):
    return post(url, "/ai/history/", {"session_id": session_id})


def clear_history(url, session_id):
    return post(url, "/ai/clear/", {"session_id": session_id})


def print_json(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def interactive(url, session_id=None, no_reasoning=False):
    print("AI chat test. Commands: message text | history | clear | quit")
    if session_id:
        print(f"session_id: {session_id}")

    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not line:
            continue
        if line in {"quit", "exit", "q"}:
            break
        if line == "history":
            if not session_id:
                print("No session yet")
                continue
            print_json(get_history(url, session_id))
            continue
        if line == "clear":
            if not session_id:
                print("No session yet")
                continue
            print_json(clear_history(url, session_id))
            continue

        data = send_message(url, line, session_id=session_id, no_reasoning=no_reasoning)
        session_id = data.get("session_id", session_id)
        print(f"session_id: {session_id}")
        print(f"reply: {data.get('reply', '')}")


def main():
    parser = argparse.ArgumentParser(description="Test /ai/send/, /ai/history/, /ai/clear/")
    parser.add_argument("--url", default=DEFAULT_URL, help="Base site URL")
    parser.add_argument("--session-id", help="Existing session id")
    parser.add_argument("-m", "--message", help="Send one message and exit")
    parser.add_argument("-i", "--image", type=Path, help="Image file for /ai/send/")
    parser.add_argument("--history", action="store_true", help="Show session history")
    parser.add_argument("--clear", action="store_true", help="Clear session history")
    parser.add_argument("--no-reasoning", action="store_true", help="Pass no_reasoning=true")
    parser.add_argument("--instructions", default="", help="System instructions")
    parser.add_argument("--interactive", action="store_true", help="Interactive chat mode")
    args = parser.parse_args()

    try:
        if args.clear:
            if not args.session_id:
                print("Pass --session-id for --clear", file=sys.stderr)
                sys.exit(1)
            print_json(clear_history(args.url, args.session_id))
            return

        if args.history:
            if not args.session_id:
                print("Pass --session-id for --history", file=sys.stderr)
                sys.exit(1)
            print_json(get_history(args.url, args.session_id))
            return

        if args.interactive or not args.message:
            interactive(args.url, session_id=args.session_id, no_reasoning=args.no_reasoning)
            return

        data = send_message(
            args.url,
            args.message,
            session_id=args.session_id,
            image_path=args.image,
            no_reasoning=args.no_reasoning,
            instructions=args.instructions,
        )
        print_json(data)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
