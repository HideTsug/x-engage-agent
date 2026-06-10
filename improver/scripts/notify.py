#!/usr/bin/env python3
"""Send a notification to Chatwork.

Drop-in replacement for the personal Discord bot used in the original project.
Engager (prompts/engager.md) and engage.sh call this to deliver the daily
like/follow candidate list to a Chatwork room.

Configuration (set by the fork owner, never committed):
  CHATWORK_API_TOKEN   Chatwork API token (https://www.chatwork.com/service/packages/chatwork/subpackages/integrations/api.php)
  CHATWORK_ROOM_ID     Target room id (the number in the room URL: /#!rid<ROOM_ID>)

These are read from the environment, or from a repo-root .env file if present.

Usage:
  notify.py "<title>" "<body>"
  notify.py "<title>" "<body>" --room 123456789   # override CHATWORK_ROOM_ID
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CHATWORK_API_BASE = "https://api.chatwork.com/v2"
REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / ".env"


def load_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env loader (KEY=VALUE lines). Avoids a hard dependency on
    python-dotenv so the notifier works with only the standard library."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def resolve(key: str, dotenv: dict[str, str]) -> str | None:
    return os.environ.get(key) or dotenv.get(key)


def send_message(title: str, body: str, room: str | None = None) -> None:
    dotenv = load_dotenv(ENV_PATH)
    token = resolve("CHATWORK_API_TOKEN", dotenv)
    room_id = room or resolve("CHATWORK_ROOM_ID", dotenv)

    if not token:
        print("Error: CHATWORK_API_TOKEN is not set", file=sys.stderr)
        sys.exit(1)
    if not room_id:
        print("Error: CHATWORK_ROOM_ID is not set (or pass --room)", file=sys.stderr)
        sys.exit(1)

    # Chatwork renders [info]/[title] blocks as a titled card.
    message = f"[info][title]{title}[/title]{body}[/info]"

    url = f"{CHATWORK_API_BASE}/rooms/{room_id}/messages"
    data = urllib.parse.urlencode({"body": message}).encode("utf-8")
    headers = {
        "X-ChatWorkToken": token,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            print(f"Message sent to room {room_id} (HTTP {resp.status})")
    except urllib.error.HTTPError as e:
        print(f"Error: {e.code} {e.read().decode()}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a notification to Chatwork")
    parser.add_argument("title", help="Message title (rendered as the card title)")
    parser.add_argument("body", help="Message body")
    parser.add_argument(
        "--room",
        default=None,
        help="Chatwork room id (overrides CHATWORK_ROOM_ID)",
    )
    args = parser.parse_args()
    send_message(args.title, args.body, room=args.room)


if __name__ == "__main__":
    main()
