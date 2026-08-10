"""One-time: capture your Telegram chat_id so the bot knows where to send digests.

Prereq: TELEGRAM_BOT_TOKEN must already be set in .env (create the bot via
@BotFather -> /newbot first). Run this, then message your bot (any text, e.g.
"/start") within a couple minutes -- it polls for that message and saves the
chat_id into preferences.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news_alert.config import TELEGRAM_BOT_TOKEN
from news_alert.db import db_cursor
from news_alert.telegram_client import get_updates


def main():
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN is not set in .env. Create a bot via @BotFather first.")
        sys.exit(1)

    with db_cursor() as cur:
        cur.execute("SELECT id FROM preferences WHERE id = 1")
        if cur.fetchone() is None:
            print("No preferences row yet. Run scripts/set_preferences.py --seed-placeholders first.")
            sys.exit(1)

    print("Message your bot on Telegram now (any text works, e.g. /start). Waiting up to 2 minutes...")

    offset = None
    for _ in range(4):  # 4 long-polls x 30s = up to 2 minutes
        updates = get_updates(offset=offset, timeout=30)
        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message")
            if message is None:
                continue
            chat_id = str(message["chat"]["id"])
            username = message["chat"].get("username") or message["chat"].get("first_name", "")
            with db_cursor() as cur:
                cur.execute(
                    "UPDATE preferences SET telegram_chat_id = ?, updated_at = ? WHERE id = 1",
                    (chat_id, datetime.now(timezone.utc).isoformat()),
                )
            print(f"Captured chat_id {chat_id} (from {username}). Saved to preferences.")
            return

    print("No message received within 2 minutes. Message your bot and re-run this script.")
    sys.exit(1)


if __name__ == "__main__":
    main()
