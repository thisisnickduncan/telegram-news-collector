"""Claude API (Haiku 4.5) summarization: headline + a few source snippets -> a
1-2 sentence SMS-safe summary. Plan section 5.4.
"""
import sys
from pathlib import Path

import anthropic

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news_alert.config import ANTHROPIC_API_KEY

MODEL = "claude-haiku-4-5"
MAX_SUMMARY_CHARS = 200
MAX_SOURCE_SNIPPETS = 3

SYSTEM_PROMPT = (
    "You write short, factual SMS news summaries. Given a story headline and, when "
    "available, a few other source headlines about the same story, write a single "
    "1-2 sentence summary in plain text. If only one headline is given, base the "
    "summary on that headline alone -- never ask for more information or refuse; "
    "always produce your best good-faith summary from what you're given. State what "
    "happened, not your opinion of it -- no fluff, no editorializing, no hashtags, no emoji. "
    f"Keep it under {MAX_SUMMARY_CHARS} characters."
)


def _build_user_message(headline, source_titles):
    distinct = [t for t in dict.fromkeys(source_titles) if t and t != headline][:MAX_SOURCE_SNIPPETS]
    message = f"Headline: {headline}"
    if distinct:
        bullets = "\n".join(f"- {t}" for t in distinct)
        message += f"\n\nOther source headlines on this story:\n{bullets}"
    else:
        message += "\n\n(No other source headlines available -- summarize based on this headline alone.)"
    return message


def summarize_story(headline, source_titles, client=None):
    client = client or anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=MODEL,
        max_tokens=200,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_user_message(headline, source_titles)}],
    )
    text = next((b.text for b in response.content if b.type == "text"), "").strip()
    if len(text) > MAX_SUMMARY_CHARS:
        text = text[:MAX_SUMMARY_CHARS - 1].rsplit(" ", 1)[0] + "…"
    return text


def summarize_pending_stories(cur, client=None):
    """Summarizes every story with summary IS NULL -- i.e. newly created this run.

    Stories that already have a summary (matched an existing story) are left as-is;
    digest.py's "update" line reuses the existing summary rather than regenerating it.
    """
    client = client or anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    cur.execute("SELECT id, headline FROM stories WHERE summary IS NULL")
    pending = cur.fetchall()

    for story in pending:
        cur.execute("SELECT title FROM story_sources WHERE story_id = ?", (story["id"],))
        source_titles = [row["title"] for row in cur.fetchall()]
        summary = summarize_story(story["headline"], source_titles, client=client)
        cur.execute("UPDATE stories SET summary = ? WHERE id = ?", (summary, story["id"]))

    return len(pending)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from news_alert.db import db_cursor

    with db_cursor() as cur:
        n = summarize_pending_stories(cur)
        cur.execute(
            "SELECT story_number, headline, summary FROM stories "
            "WHERE summary IS NOT NULL ORDER BY story_number"
        )
        rows = cur.fetchall()

    print(f"Summarized {n} stories.\n")
    for r in rows:
        print(f"#{r['story_number']} {r['headline']}")
        print(f"   -> {r['summary']}\n")
