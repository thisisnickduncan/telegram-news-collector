"""Parses Telegram inline-button presses: follow:<id>, stop:<id>, ask:<id>.
Plan section 5.7 (extended with "ask" for the per-story Q&A feature).
"""
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news_alert.telegram_client import (
    answer_callback_query,
    edit_message_reply_markup,
    safe_call,
    send_message,
)

CALLBACK_RE = re.compile(r"^(follow|stop|ask):(\d+)$")


def _flip_keyboard_button(callback_query, story_id, following):
    """Rewrites just the one button whose callback_data matches this story's
    follow/stop action, in place, then pushes the whole keyboard back --
    editMessageReplyMarkup replaces the full markup, there's no partial-patch API."""
    message = callback_query["message"]
    keyboard = message.get("reply_markup", {}).get("inline_keyboard", [])
    for row in keyboard:
        for button in row:
            cb = button.get("callback_data", "")
            if cb in (f"follow:{story_id}", f"stop:{story_id}"):
                if following:
                    button["text"] = "Following ✓ (tap to stop)"
                    button["callback_data"] = f"stop:{story_id}"
                else:
                    button["text"] = "Follow (tap to resume)"
                    button["callback_data"] = f"follow:{story_id}"

    chat_id = message["chat"]["id"]
    message_id = message["message_id"]
    safe_call(edit_message_reply_markup, chat_id, message_id, {"inline_keyboard": keyboard})


def handle_callback_query(cur, callback_query):
    data = callback_query.get("data", "")
    print(f"[callback_handler] received callback_data={data!r}")
    match = CALLBACK_RE.match(data)
    if not match:
        print(f"[callback_handler] {data!r} didn't match {CALLBACK_RE.pattern} -- ignoring")
        safe_call(answer_callback_query, callback_query["id"], text="Unrecognized button.")
        return

    action, story_id = match.group(1), int(match.group(2))
    print(f"[callback_handler] action={action!r} story_id={story_id}")
    now = datetime.now(timezone.utc).isoformat()

    if action == "follow":
        cur.execute(
            "INSERT INTO follows (story_id, followed_at) VALUES (?, ?) "
            "ON CONFLICT(story_id) DO UPDATE SET followed_at = excluded.followed_at",
            (story_id, now),
        )
        cur.execute("UPDATE stories SET status = 'followed' WHERE id = ?", (story_id,))
        safe_call(answer_callback_query, callback_query["id"],
                  text="Following. You'll get updates on this story.")
        _flip_keyboard_button(callback_query, story_id, following=True)

    elif action == "stop":
        cur.execute("DELETE FROM follows WHERE story_id = ?", (story_id,))
        cur.execute("UPDATE stories SET status = 'active' WHERE id = ?", (story_id,))
        safe_call(answer_callback_query, callback_query["id"], text="Stopped.")
        _flip_keyboard_button(callback_query, story_id, following=False)

    elif action == "ask":
        cur.execute("SELECT headline FROM stories WHERE id = ?", (story_id,))
        story = cur.fetchone()
        if story is None:
            safe_call(answer_callback_query, callback_query["id"],
                      text="That story isn't available anymore.")
            return
        cur.execute("UPDATE preferences SET pending_ask_story_id = ? WHERE id = 1", (story_id,))
        safe_call(answer_callback_query, callback_query["id"])
        chat_id = callback_query["message"]["chat"]["id"]
        headline = (story["headline"] or "").strip()
        safe_call(send_message, chat_id, f'What would you like to know about "{headline}"?',
                  parse_mode=None)
