"""Deciding whether a story you were already sent has actually moved.

A delivered story used to come back for one reason: it was sent as single-source and had
since picked up a second outlet, so it reappeared marked "Now corroborated". That is a
fact about our confidence, not about the news, and from the reading end it is simply the
same story a second time.

The rule now is the one a person would state: once you've been sent something, it comes
back only if something *happened*. More outlets running the same piece is not something
happening. Neither is a recap, a reaction column, or a new number attached to a fact you
were already given.

Answering that needs to compare what you were told against what has arrived since, which
is a judgement, not a count -- so it is one batched Haiku call per run over the handful of
delivered stories that picked up anything at all. Stories with no new sources never reach
the call, so a quiet cycle costs nothing.

The same gate covers followed stories. Following used to mean an update on *any* new
article, which fired on syndicated reprints and sent "↻ Update" messages carrying no
update. Following now means you get the developments; it never meant you wanted to be
pinged because a wire piece was republished.
"""
import re
import sys
from pathlib import Path

import anthropic

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news_alert.config import ANTHROPIC_API_KEY

MODEL = "claude-haiku-4-5"

# Stories examined per run. Ordered by how much new coverage arrived, so the cap drops
# the stories least likely to have moved.
MAX_STORIES = 20
# New headlines shown per story. Beyond a handful they repeat each other.
MAX_NEW_TITLES = 8

SYSTEM_PROMPT = (
    "A reader was sent a short summary of a news story. More coverage of that story has "
    "arrived since. Decide whether anything actually HAPPENED that they have not been "
    "told.\n\n"
    "Something happened when the new coverage reports a concrete change of fact: a winner "
    "where there was none, a ruling issued, a death toll revised, an arrest, a "
    "resignation, a deal signed, a vote taken, a figure passing a threshold that matters.\n\n"
    "Nothing happened when the new coverage merely: repeats what the reader was told in "
    "different words; is the same wire story on more outlets; adds background, analysis, "
    "opinion, reaction or commentary; recaps the story for a new audience; or restates a "
    "known fact with a rounder number.\n\n"
    "Output exactly one line per numbered story:\n"
    "  <number>: SAME\n"
    "or\n"
    "  <number>: NEW <one plain sentence saying what happened>\n"
    "The sentence must state the new fact itself, not that there is news -- \"A single "
    "ticket sold in Texas won the $1.1 billion jackpot\", never \"there has been a "
    "development in the lottery story\". No other text.\n\n"
    "Default to SAME. Most stories in a batch will be SAME and that is the correct "
    "answer. A wrong NEW sends the reader the same story twice, which is the exact "
    "annoyance this check exists to prevent."
)

_LINE_RE = re.compile(r"^\s*(\d+)\s*[:.\)-]\s*(SAME|NEW)\b[\s:-]*(.*)$", re.IGNORECASE)


def _client(client=None):
    return client or anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def candidates_with_new_coverage(cur, max_stories=MAX_STORIES, max_age_hours=None):
    """Delivered stories carrying sources fetched since the last thing we told the user.

    The watermark is `development_at` when a development was already reported, otherwise
    `delivered_at` -- so a story that develops twice is judged against the second state,
    not the original one, and can't re-report the first development forever.
    """
    age_clause = ""
    params = []
    if max_age_hours is not None:
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        age_clause = "AND ss.fetched_at >= ?"
        params.append(cutoff)

    cur.execute(
        f"""SELECT s.id AS id, s.headline AS headline, s.summary AS summary,
                   s.development_summary AS development_summary,
                   s.status AS status,
                   COUNT(ss.id) AS fresh
            FROM stories s
            JOIN story_sources ss ON ss.story_id = s.id
           WHERE s.delivered_at IS NOT NULL
             AND s.status NOT IN ('expired', 'muted', 'duplicate')
             AND ss.fetched_at > COALESCE(s.development_at, s.delivered_at)
             {age_clause}
           GROUP BY s.id
           ORDER BY fresh DESC
           LIMIT ?""",
        tuple(params) + (max_stories,),
    )
    return cur.fetchall()


def _new_titles(cur, story_id):
    cur.execute(
        """SELECT DISTINCT ss.title
             FROM story_sources ss JOIN stories s ON s.id = ss.story_id
            WHERE ss.story_id = ?
              AND ss.title IS NOT NULL
              AND ss.fetched_at > COALESCE(s.development_at, s.delivered_at)
            LIMIT ?""",
        (story_id, MAX_NEW_TITLES),
    )
    return [row["title"] for row in cur.fetchall() if row["title"]]


def _parse(reply, count):
    developments = {}
    for line in reply.splitlines():
        match = _LINE_RE.match(line)
        if not match:
            continue
        index = int(match.group(1)) - 1
        if not 0 <= index < count:
            continue
        if match.group(2).upper() == "NEW":
            fact = match.group(3).strip().strip("\"'")
            if fact:
                developments[index] = fact
    return developments


def pending_developments(cur, client=None, max_stories=MAX_STORIES, max_age_hours=None):
    """{story_id: one-sentence new fact} for delivered stories that genuinely moved.

    Empty when nothing picked up coverage, and empty on any failure -- a broken call
    means no updates go out, which is the quiet failure. Failing the other way would
    resend stories, the exact thing this gate exists to stop.
    """
    rows = candidates_with_new_coverage(cur, max_stories=max_stories,
                                        max_age_hours=max_age_hours)
    items = []
    for row in rows:
        titles = _new_titles(cur, row["id"])
        told = (row["development_summary"] or row["summary"] or row["headline"] or "").strip()
        if titles and told:
            items.append((row["id"], told, titles))

    if not items:
        return {}

    blocks = []
    for i, (_, told, titles) in enumerate(items, start=1):
        headlines = "\n".join(f"   - {title}" for title in titles)
        blocks.append(f"{i}. READER WAS TOLD: {told}\n   NEW COVERAGE:\n{headlines}")

    try:
        response = _client(client).messages.create(
            model=MODEL,
            max_tokens=60 * len(items) + 200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": "\n\n".join(blocks)}],
        )
    except Exception as exc:
        print(f"[developments] check failed, sending no updates this run: {exc}")
        return {}

    reply = next((b.text for b in response.content if b.type == "text"), "").strip()
    developments = {}
    for index, fact in _parse(reply, len(items)).items():
        story_id = items[index][0]
        developments[story_id] = fact
        print(f"[developments] story {story_id} developed: {fact[:80]!r}")

    print(f"[developments] {len(developments)}/{len(items)} delivered stories with new "
          f"coverage actually moved.")
    return developments


def record_development(cur, story_id, fact, when):
    """Marks the development as told, which also moves the watermark for the next check."""
    cur.execute(
        "UPDATE stories SET development_summary = ?, development_at = ? WHERE id = ?",
        (fact, when, story_id),
    )
