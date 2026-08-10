"""Orchestration: fetch -> dedupe -> bias tag -> summarize -> compose -> send.
Plan section 5.8. Runs top to bottom, logs each stage, exits.
"""
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news_alert.bias import tag_untagged_sources
from news_alert.db import get_preferences, db_cursor
from news_alert.dedupe import dedupe_articles
from news_alert.digest import compose_digest
from news_alert.fetcher import fetch_all
from news_alert.summarizer import summarize_pending_stories
from news_alert.telegram_client import send_message


def run():
    prefs = get_preferences()
    if prefs is None:
        print("[pipeline] No preferences set. Run scripts/set_preferences.py first.")
        return
    if not prefs["telegram_chat_id"]:
        print("[pipeline] No telegram_chat_id set. Run scripts/register_bot.py first.")
        return

    print("[pipeline] Fetching...")
    articles = fetch_all(prefs)
    print(f"[pipeline] Fetched {len(articles)} articles. Deduping...")
    results = dedupe_articles(articles)

    with db_cursor() as cur:
        tagged = tag_untagged_sources(cur)
    print(f"[pipeline] Tagged {tagged} sources with bias ratings.")

    print("[pipeline] Summarizing new stories...")
    with db_cursor() as cur:
        summarized = summarize_pending_stories(cur)
    print(f"[pipeline] Summarized {summarized} stories.")

    with db_cursor() as cur:
        digest = compose_digest(cur, results, max_stories=prefs["digest_max_stories"])

    if digest is None:
        print("[pipeline] Nothing new, no followed-story updates. Not sending.")
        return

    print("[pipeline] Sending digest...")
    sent = send_message(prefs["telegram_chat_id"], digest["text"], reply_markup=digest["reply_markup"])
    print(f"[pipeline] Sent. message_id={sent['message_id']}")

    new_story_ids = sorted({r["story_id"] for r in results if r["action"] == "new"})
    shown = new_story_ids[:prefs["digest_max_stories"]]
    with db_cursor() as cur:
        for story_id in shown:
            cur.execute(
                "UPDATE stories SET telegram_message_id = ? WHERE id = ?",
                (str(sent["message_id"]), story_id),
            )


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    run()
