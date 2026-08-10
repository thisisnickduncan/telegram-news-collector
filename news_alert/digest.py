"""Digest composition: turn one run's dedupe results into a Telegram message.
Plan sections 5.5 / 6. No telegram_client calls here -- this just builds
{"text", "reply_markup"}; telegram_client.send_message() does the sending (phase 4).
"""
import html
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news_alert.bias import story_bias_spread
from news_alert.config import DISPLAY_TIMEZONE


def _format_time(now):
    hour = now.hour % 12 or 12
    ampm = "am" if now.hour < 12 else "pm"
    return f"{hour}:{now.strftime('%M')}{ampm}"


def _escape(text):
    # quote=False: Telegram's HTML mode only requires escaping & < > -- escaping quotes
    # too (html.escape's default) renders literal "&#x27;" for every apostrophe.
    return html.escape(text, quote=False)


def _story_line(cur, story_id, index):
    cur.execute("SELECT headline, summary FROM stories WHERE id = ?", (story_id,))
    story = cur.fetchone()
    spread = story_bias_spread(cur, story_id)
    tag = "/".join(spread) if spread else "Unrated"

    cur.execute(
        "SELECT DISTINCT outlet_name, domain FROM story_sources WHERE story_id = ?",
        (story_id,),
    )
    outlets = [row["outlet_name"] or row["domain"] for row in cur.fetchall()]
    source_line = ", ".join(outlets)
    if len(spread) > 1:
        source_line += " — mixed coverage"

    # Prefer the Claude-cleaned summary over the raw (often messily-scraped) GDELT headline.
    text = story["summary"] or story["headline"]
    return f"{index}. [{tag}] <b>{_escape(text)}</b>\n   {_escape(source_line)}"


def _update_line(cur, story_id, label):
    cur.execute("SELECT headline, summary FROM stories WHERE id = ?", (story_id,))
    row = cur.fetchone()
    blurb = row["summary"] or "new development"
    headline = (row["headline"] or "").strip()
    return f'↻ {label}. Update on "{_escape(headline)}" (followed):\n   {_escape(blurb)}'


def compose_digest(cur, dedupe_results, max_stories=6, now=None):
    """Builds {"text": str, "reply_markup": dict|None} from one dedupe_articles() run.

    Returns None when there's nothing new and no followed-story updates -- the
    pipeline should skip sending entirely in that case (plan section 5.5).
    reply_markup is a Telegram inline_keyboard: one row per shown story, each with a
    "Follow #N" + "Ask #N" button pair for new stories (Ask-only for followed/update
    stories, since they're already followed). Buttons render below the whole message,
    not next to each entry, so the number is how you tell them apart.
    """
    new_ids = sorted({r["story_id"] for r in dedupe_results if r["action"] == "new"})
    update_ids = sorted({r["story_id"] for r in dedupe_results if r["action"] == "update"})

    if not new_ids and not update_ids:
        return None

    shown_new = new_ids[:max_stories]
    overflow = len(new_ids) - len(shown_new)

    # Must be timezone-aware: the server runs on UTC, so a naive now() would label
    # the 8:00pm Pacific digest "3:00am".
    now = now or datetime.now(ZoneInfo(DISPLAY_TIMEZONE))
    update_word = "update" if len(update_ids) == 1 else "updates"
    intro = (
        "\U0001F44B Here's what's happening in your tracked topics -- tap Ask on any "
        "story to dig deeper, or just message me to start tracking something new."
    )
    header = f"\U0001F4F0 News — {_format_time(now)} ({len(new_ids)} new, {len(update_ids)} {update_word})"

    lines = [intro, "", header, ""]
    buttons = []
    for i, story_id in enumerate(shown_new, start=1):
        lines.append(_story_line(cur, story_id, i))
        lines.append("")
        buttons.append([
            {"text": f"Follow #{i}", "callback_data": f"follow:{story_id}"},
            {"text": f"Ask #{i}", "callback_data": f"ask:{story_id}"},
        ])

    if overflow > 0:
        lines.append(f"+{overflow} more, refine your topics")
        lines.append("")

    if update_ids:
        for j, story_id in enumerate(update_ids, start=1):
            label = f"U{j}"
            lines.append(_update_line(cur, story_id, label))
            lines.append("")
            buttons.append([{"text": f"Ask {label}", "callback_data": f"ask:{story_id}"}])

    text = "\n".join(lines).rstrip()
    reply_markup = {"inline_keyboard": buttons} if buttons else None

    return {"text": text, "reply_markup": reply_markup}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from news_alert.bias import tag_untagged_sources
    from news_alert.db import get_preferences, db_cursor
    from news_alert.dedupe import dedupe_articles
    from news_alert.fetcher import fetch_all
    from news_alert.summarizer import summarize_pending_stories

    prefs = get_preferences()
    if prefs is None:
        print("No preferences set. Run scripts/set_preferences.py first.")
        sys.exit(1)

    print("Fetching...")
    articles = fetch_all(prefs)
    print(f"\nFetched {len(articles)} articles. Deduping...")
    results = dedupe_articles(articles)

    with db_cursor() as cur:
        tagged = tag_untagged_sources(cur)
        print(f"Tagged {tagged} sources with bias ratings.")

    print("Summarizing new stories via Claude...")
    with db_cursor() as cur:
        summarized = summarize_pending_stories(cur)
    print(f"Summarized {summarized} stories.\n")

    with db_cursor() as cur:
        digest = compose_digest(cur, results, max_stories=prefs["digest_max_stories"])

    print("=" * 40)
    if digest is None:
        print("Nothing new, no followed-story updates -- would skip sending.")
    else:
        print(digest["text"])
        print("\n--- reply_markup ---")
        print(digest["reply_markup"])
    print("=" * 40)
