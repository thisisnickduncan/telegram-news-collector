"""Grouping a run's articles by the real-world event they describe.

This exists because lexical similarity cannot find corroboration. Measured on one live
run: three newsrooms covering Iran's appointment of Mohsen Rezaei to the Supreme
National Security Council produced headlines scoring 52-57 against each other, while
dedupe's threshold is 80 -- so they became three separate single-source stories. Four
more did the same for one Burnham announcement. Meanwhile syndicated reprints of a
single wire piece score ~100 and cluster perfectly.

That gap is structural, not a tuning problem:

    same event, different newsrooms   44-67
    same article, republished         ~100
    threshold                         80

No threshold separates the first row from unrelated stories, so lowering it trades
missed corroboration for false merges. The signal has to be semantic, so one cheap
Haiku call per batch reads the headlines and says which describe the same event.

The call also sees recent stories for the topic, so an event can attach to a story from
an earlier cycle -- which is what makes "hold a single-source story and ship it once it
picks up corroboration" work across runs rather than only within one.

Every failure path falls back to the previous behaviour (per-article fuzzy matching in
dedupe.py). A bad API call degrades this to what it was before, never worse.
"""
import re
import sys
from pathlib import Path

import anthropic

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news_alert.config import ANTHROPIC_API_KEY

MODEL = "claude-haiku-4-5"

# Headlines per call. Small enough that the model reliably attends to every line and
# emits one output row each; a full 250-record topic simply takes a few calls. Groups
# found in one chunk are passed into the next as existing events, so chunking doesn't
# split an event.
MAX_HEADLINES_PER_CALL = 40
# Cap on how many prior stories are offered as attachment targets, newest first.
MAX_EXISTING_PER_CALL = 50

SYSTEM_PROMPT = (
    "You group news headlines by the underlying real-world EVENT they report.\n\n"
    "Two headlines belong to the same event when they describe the same occurrence -- the "
    "same appointment, the same vote, the same attack, the same announcement -- even when "
    "they share almost no words. \"Iran names new head of top national security body\" and "
    "\"Former IRGC commander Mohsen Rezaei appointed Supreme National Security Council "
    "chief\" are the SAME event.\n\n"
    "They are DIFFERENT events when they merely share a topic, a country, or a person. Two "
    "separate strikes are two events. A vote and a later reaction to that vote are two "
    "events. An ongoing conflict is not one event -- group by the specific occurrence.\n\n"
    "You may be given EXISTING events with ids like E12 or G3. If a headline reports the "
    "same event as an existing one, assign it that exact id. Otherwise assign it a new "
    "group id you invent, of the form N1, N2, N3 -- reuse the same new id for headlines "
    "that belong together.\n\n"
    "CRITICAL: most headlines in a batch belong to no group but themselves. A typical "
    "batch is mostly unique events with a handful of small clusters. Never assign a "
    "headline to a group merely because nothing else fits it -- an unmatched headline "
    "gets its own new id, and that is the correct, expected outcome for the majority of "
    "them. Putting an unrelated headline into a group is the single worst mistake you "
    "can make here; a missed grouping costs almost nothing.\n\n"
    "Headlines in different languages CAN be the same event, but only when they report "
    "the same specific occurrence -- not merely the same country, theme or industry.\n\n"
    "Output format: exactly one line per input headline, in input order, as\n"
    "  <headline number>: <event id>\n"
    "for example:\n"
    "  1: N1\n"
    "  2: E12\n"
    "  3: N1\n"
    "Output nothing else -- no commentary, no blank lines, no explanation. Every input "
    "number must appear exactly once."
)

_LINE_RE = re.compile(r"^\s*(\d+)\s*[:.\)-]\s*([EGN]\d+)\s*$", re.IGNORECASE)


def _parse(reply, count):
    """Maps 'n: E12' lines back to a per-headline list of event ids. Unparseable or
    missing lines come back as None, which the caller treats as "not grouped"."""
    assigned = [None] * count
    for line in reply.splitlines():
        match = _LINE_RE.match(line)
        if not match:
            continue
        index = int(match.group(1)) - 1
        if 0 <= index < count:
            assigned[index] = match.group(2).upper()
    return assigned


def _recent_stories(cur, topic, limit=MAX_EXISTING_PER_CALL):
    """Stories for this topic that a new article could still be reporting on.

    Restricted to stories still inside the corroboration window -- attaching tonight's
    article to a week-old story would resurrect it, and pipeline.py would filter it out
    on staleness anyway.
    """
    cur.execute(
        """SELECT id, headline FROM stories
           WHERE topic = ? AND status != 'expired' AND headline IS NOT NULL
           ORDER BY last_updated_at DESC LIMIT ?""",
        (topic, limit),
    )
    return cur.fetchall()


def _call_model(client, offered, headlines):
    """offered: list of (id_string, headline) already carrying its E.. / G.. prefix."""
    lines = []
    if offered:
        lines.append("EXISTING EVENTS:")
        lines.extend(f"{event_id}: {headline}" for event_id, headline in offered)
        lines.append("")
    lines.append("HEADLINES TO ASSIGN:")
    lines.extend(f"{i}. {title}" for i, title in enumerate(headlines, start=1))

    response = client.messages.create(
        model=MODEL,
        max_tokens=16 * len(headlines) + 200,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": "\n".join(lines)}],
    )
    return next((b.text for b in response.content if b.type == "text"), "")


def assign_events(cur, articles, client=None):
    """Returns a list parallel to `articles`: an int story id to attach to, a string
    group key shared by articles of one new event, or None to fall back to fuzzy matching.

    Articles are batched per topic, because dedupe scopes matching by topic and mixing
    topics in one call would invite cross-topic merges.
    """
    assignments = [None] * len(articles)
    by_topic = {}
    for i, article in enumerate(articles):
        if article.get("title"):
            by_topic.setdefault(article["topic"], []).append(i)

    if not by_topic:
        return assignments

    client = client or anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    for topic, indices in by_topic.items():
        existing = [(f"E{row['id']}", row["headline"], row["id"])
                    for row in _recent_stories(cur, topic)]
        # Groups found in earlier chunks are offered to later ones under G ids, so an
        # event split across a chunk boundary still ends up as one story. They need
        # their own id space: the model re-invents N1, N2... in every chunk, and chunk
        # 2's N1 is a different event from chunk 1's N1. Keying new groups on the raw
        # N-id merged them -- which is how a Manchester cost-of-living announcement
        # ended up inside the Iran cluster.
        pending = []          # list of (group_key, representative_headline)

        for chunk_no, start in enumerate(range(0, len(indices), MAX_HEADLINES_PER_CALL), 1):
            chunk = indices[start:start + MAX_HEADLINES_PER_CALL]
            headlines = [articles[i]["title"] for i in chunk]

            offered = [(eid, headline) for eid, headline, _ in existing[:MAX_EXISTING_PER_CALL]]
            group_ids = {}
            for n, (key, headline) in enumerate(pending, start=1):
                gid = f"G{n}"
                group_ids[gid] = key
                offered.append((gid, headline))

            try:
                reply = _call_model(client, offered, headlines)
            except Exception as exc:
                # Whole chunk falls through to fuzzy matching -- the old behaviour.
                print(f"[events] grouping call failed for topic={topic!r}: {exc}")
                continue

            parsed = _parse(reply, len(headlines))
            matched = sum(1 for value in parsed if value)
            print(f"[events] topic={topic!r} chunk {chunk_no}: "
                  f"{matched}/{len(headlines)} headlines assigned")

            story_ids = {eid for eid, _, _ in existing}
            for position, event_id in enumerate(parsed):
                if not event_id:
                    continue
                article_index = chunk[position]
                if event_id.startswith("E") and event_id in story_ids:
                    assignments[article_index] = int(event_id[1:])
                elif event_id.startswith("G") and event_id in group_ids:
                    assignments[article_index] = group_ids[event_id]
                elif event_id.startswith("N"):
                    # Namespaced by topic AND chunk -- see the comment on `pending`.
                    key = f"{topic}::c{chunk_no}::{event_id}"
                    assignments[article_index] = key
                    if key not in dict(pending):
                        pending.append((key, articles[article_index]["title"]))
                # Anything else (a hallucinated id) stays None -> fuzzy fallback.

    return assignments


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from news_alert.db import db_cursor

    # The real headlines from the run that motivated this module.
    sample = [
        "Rezaei made Mojtaba representative to national security council",
        "Iran names new head of top national security body",
        "Former IRGC commander Mohsen Rezaei appointed Supreme National Security Council chief",
        "Russia , Ukraine exchange overnight strikes that leave 7 dead , many injured",
        "World food prices hit three - year high in July - FAO",
        "UK PM Andy Burnham vows crackdown on subscription traps , fake discounts",
        "Burnham to promise everyday fixes for bills at start of cost - of - living tour",
    ]
    articles = [{"title": t, "topic": "Iran war"} for t in sample]
    with db_cursor() as cur:
        result = assign_events(cur, articles)
    for article, event in zip(articles, result):
        print(f"  {str(event):28} {article['title'][:70]}")
