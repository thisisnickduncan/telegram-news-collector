"""Orchestration: fetch -> dedupe -> bias tag -> summarize -> compose -> hold -> send.
Plan section 5.8. Runs top to bottom, logs each stage, exits.
"""
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news_alert.bias import tag_untagged_sources
from news_alert.db import get_preferences, db_cursor
from news_alert.dedupe import dedupe_articles
from news_alert.digest import compose_digest
from news_alert.fetcher import fetch_all
from news_alert.summarizer import summarize_pending_stories
from news_alert.telegram_client import safe_call, send_message


def _notify(prefs, text):
    """Best-effort operator alert, to the same chat the digest goes to. Never raises --
    a failed alert must not also take down the run it was reporting on."""
    chat_id = prefs.get("telegram_chat_id")
    if not chat_id:
        return
    try:
        safe_call(send_message, chat_id, text, parse_mode=None)
    except Exception:
        traceback.print_exc()


def _hold_until(send_at):
    """Sleep until the exact target send time.

    Runs on the scheduler's own worker thread, so Telegram long-polling in the main
    thread keeps answering buttons and messages throughout the hold.
    """
    delay = (send_at - datetime.now(send_at.tzinfo)).total_seconds()
    if delay > 0:
        print(f"[pipeline] Digest ready; holding {delay:.0f}s to send at {send_at:%H:%M:%S %Z}.")
        time.sleep(delay)
    else:
        print(f"[pipeline] Digest ready, but {send_at:%H:%M:%S %Z} passed {-delay:.0f}s ago "
              f"-- sending immediately.")


def run(send_at=None):
    """send_at: an aware datetime to hold the finished digest for, or None to send
    as soon as it's ready.

    The fetch takes anywhere from ~1 to ~13 minutes depending purely on how many times
    GDELT 429s at us, so firing the job at the target time makes delivery drift by that
    much -- an 8:00pm digest once arrived at 8:26pm. bot_runner starts the job
    LEAD_MINUTES early and passes the real target here, so the variable part happens
    inside the lead and the message still lands on the hour.
    """
    prefs = get_preferences()
    if prefs is None:
        print("[pipeline] No preferences set. Run scripts/set_preferences.py first.")
        return
    if not prefs["telegram_chat_id"]:
        print("[pipeline] No telegram_chat_id set. Run scripts/register_bot.py first.")
        return

    print("[pipeline] Fetching...")
    articles = fetch_all(prefs)
    if not articles:
        # Distinct from "nothing new": that means the fetch worked and there was no
        # news. This means every query struck out and we never got data at all, which
        # otherwise fails completely silently -- the digest just never arrives.
        print("[pipeline] Fetch returned 0 articles -- every query failed. Alerting.")
        _notify(prefs, "news-alert: GDELT returned nothing this cycle -- every query "
                       "failed or came back empty. No digest sent; check the logs.")
        return

    print(f"[pipeline] Fetched {len(articles)} articles. Deduping...")
    results = dedupe_articles(articles)

    with db_cursor() as cur:
        tagged = tag_untagged_sources(cur)
    print(f"[pipeline] Tagged {tagged} sources with bias ratings.")

    # Only summarize what the digest will actually show. compose_digest() picks the
    # same slice -- sorted new story ids, capped at digest_max_stories -- so this must
    # stay in step with it. Followed-story updates are included because an update line
    # reuses the story's existing summary, which may be missing if an earlier run
    # skipped it.
    new_story_ids = sorted({r["story_id"] for r in results if r["action"] == "new"})
    update_story_ids = sorted({r["story_id"] for r in results if r["action"] == "update"})
    shown = new_story_ids[:prefs["digest_max_stories"]]

    print(f"[pipeline] Summarizing {len(shown)} shown + {len(update_story_ids)} followed "
          f"(of {len(new_story_ids)} new stories)...")
    with db_cursor() as cur:
        summarized = summarize_pending_stories(cur, story_ids=shown + update_story_ids)
    print(f"[pipeline] Summarized {summarized} stories.")

    # now=send_at so the header reads the time the digest will actually arrive, not
    # the time it happened to finish composing.
    with db_cursor() as cur:
        digest = compose_digest(cur, results, max_stories=prefs["digest_max_stories"],
                                now=send_at)

    if digest is None:
        print("[pipeline] Nothing new, no followed-story updates. Not sending.")
        return

    if send_at is not None:
        _hold_until(send_at)

    print("[pipeline] Sending digest...")
    sent = send_message(prefs["telegram_chat_id"], digest["text"], reply_markup=digest["reply_markup"])
    print(f"[pipeline] Sent. message_id={sent['message_id']}")

    with db_cursor() as cur:
        for story_id in shown:
            cur.execute(
                "UPDATE stories SET telegram_message_id = ? WHERE id = ?",
                (str(sent["message_id"]), story_id),
            )


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    run()
