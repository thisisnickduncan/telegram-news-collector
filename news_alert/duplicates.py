"""Catching the same event arriving twice as two different stories.

events.py already groups a run's headlines into events, but it batches *per topic* --
and it has to, because dedupe.py scopes its fuzzy match by topic too and mixing topics
in one grouping call invites cross-topic merges. The consequence is structural: one
real-world event that matches two of your search terms (or a topic and a region) becomes
two story rows that can never see each other, and both then clear the corroboration bar
independently and both ship.

That is how one Powerball drawing went out twice in a single digest, worded differently,
with different source lists. No amount of tuning inside events.py can fix it -- the two
rows are never in the same call.

So this runs at the other end of the pipeline, on the handful of stories about to be
sent, where cross-topic comparison is cheap and safe:

  * the candidate set is ~30 stories, not the four-figure held pool;
  * they're compared against each other AND against what was sent recently, which is the
    same bug showing up across digests rather than within one;
  * a duplicate is dropped from the digest and marked, never merged into the survivor.
    Merging would rewrite source attribution on the strength of one model call; dropping
    understates a story's voice count, which is the safe direction to be wrong in.

Conservative by construction: a false positive here silently deletes a story the user
would have wanted, while a miss just means they see something twice -- which is exactly
the failure they can spot and report.
"""
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news_alert.config import ANTHROPIC_API_KEY

MODEL = "claude-haiku-4-5"

# How far back delivered stories are offered as "already sent". Longer than the 24h
# candidate window on purpose: a story held, delivered, then re-created under a second
# topic can surface a day later, and the point is to catch exactly that.
SENT_LOOKBACK_HOURS = 48
MAX_SENT = 40
MAX_CANDIDATES = 40

SYSTEM_PROMPT = (
    "You are checking a personal news digest for the same story appearing twice.\n\n"
    "You get a list of stories ALREADY SENT to this reader, then a numbered list of "
    "CANDIDATES about to be sent. Find candidates that report the SAME real-world "
    "occurrence as an already-sent story, or as an earlier candidate in the list.\n\n"
    "Same occurrence means the same drawing, the same vote, the same strike, the same "
    "announcement, the same ruling -- even when the wording, the framing and the numbers "
    "quoted are different, and even when the two are filed under different subjects.\n\n"
    "NOT the same: two events in one conflict, a vote and the reaction to it, two "
    "separate quarters of results, two different rulings in one case, a later development "
    "in a running story. Sharing a country, a person, a company or a theme is NOT enough. "
    "If one text reports something the other does not, they are different events.\n\n"
    "Output one line per duplicate, formatted exactly:\n"
    "  <candidate number>: <the item it duplicates>\n"
    "where the item is either P<number> for an already-sent story or a plain number for "
    "an earlier candidate. Report each duplicate once, against the earliest item it "
    "matches. If there are no duplicates, output exactly: NONE\n"
    "No other text.\n\n"
    "Report a duplicate only when you are confident it is literally the same occurrence. "
    "A wrongly flagged story is deleted and the reader never learns it existed; a missed "
    "one merely repeats. When unsure, say nothing."
)

_LINE_RE = re.compile(r"^\s*(\d+)\s*[:.\)-]\s*(P?\s*\d+)\s*$", re.IGNORECASE)


def _client(client=None):
    return client or anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def _text_for(row):
    return ((row["summary"] if "summary" in row.keys() else None)
            or row["headline"] or "").strip()


def recently_sent(cur, hours=SENT_LOOKBACK_HOURS, limit=MAX_SENT):
    """Stories already delivered inside the lookback window, newest first."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    cur.execute(
        """SELECT id, headline, summary FROM stories
           WHERE delivered_at IS NOT NULL AND delivered_at >= ?
           ORDER BY delivered_at DESC LIMIT ?""",
        (cutoff, limit),
    )
    return [(row["id"], _text_for(row)) for row in cur.fetchall() if _text_for(row)]


def _parse(reply, candidate_count, sent_count):
    """'3: P2' / '5: 1' -> {candidate index: ('sent'|'candidate', index)}, 0-based."""
    found = {}
    for line in reply.splitlines():
        match = _LINE_RE.match(line)
        if not match:
            continue
        index = int(match.group(1)) - 1
        target = match.group(2).replace(" ", "").upper()
        if not 0 <= index < candidate_count:
            continue
        if target.startswith("P"):
            other = int(target[1:]) - 1
            if 0 <= other < sent_count:
                found[index] = ("sent", other)
        else:
            other = int(target) - 1
            # Only backwards references: "2 duplicates 5" would otherwise let the model
            # drop the better-ranked story and keep the worse one.
            if 0 <= other < index:
                found[index] = ("candidate", other)
    return found


def find_duplicates(cur, candidate_ids, client=None, sent=None):
    """Which of `candidate_ids` repeat something.

    candidate_ids must be in send order -- the earlier of a pair is the one kept, so the
    caller's ranking decides which copy survives.

    Returns {duplicate_story_id: original_story_id}, where the original may be a story
    already sent or an earlier candidate. Empty on any failure: an unavailable model call
    must degrade to today's behaviour (a possible repeat), never to a dropped digest.
    """
    candidate_ids = list(dict.fromkeys(candidate_ids))[:MAX_CANDIDATES]
    if len(candidate_ids) < 2 and not sent:
        return {}

    placeholders = ",".join("?" * len(candidate_ids))
    cur.execute(
        f"SELECT id, headline, summary, topic FROM stories WHERE id IN ({placeholders})",
        tuple(candidate_ids),
    )
    rows = {row["id"]: row for row in cur.fetchall()}
    candidates = [(sid, _text_for(rows[sid]), rows[sid]["topic"])
                  for sid in candidate_ids if sid in rows and _text_for(rows[sid])]
    if not candidates:
        return {}

    sent = recently_sent(cur) if sent is None else list(sent)
    # A story can't duplicate itself: candidates that were delivered before (they're here
    # because they developed) would otherwise match their own already-sent entry.
    candidate_id_set = {sid for sid, _, _ in candidates}
    sent = [(sid, text) for sid, text in sent if sid not in candidate_id_set]

    if len(candidates) < 2 and not sent:
        return {}

    lines = []
    if sent:
        lines.append("ALREADY SENT:")
        lines.extend(f"P{i}. {text}" for i, (_, text) in enumerate(sent, start=1))
        lines.append("")
    lines.append("CANDIDATES:")
    lines.extend(
        f"{i}. {text}" + (f"  [subject: {topic}]" if topic else "")
        for i, (_, text, topic) in enumerate(candidates, start=1)
    )

    try:
        response = _client(client).messages.create(
            model=MODEL,
            max_tokens=8 * len(candidates) + 200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": "\n".join(lines)}],
        )
    except Exception as exc:
        print(f"[duplicates] check failed, sending everything: {exc}")
        return {}

    reply = next((b.text for b in response.content if b.type == "text"), "").strip()
    if reply.upper().startswith("NONE"):
        return {}

    duplicates = {}
    for index, (kind, other) in _parse(reply, len(candidates), len(sent)).items():
        story_id, text, _ = candidates[index]
        original_id = sent[other][0] if kind == "sent" else candidates[other][0]
        # Chase a chain (3 duplicates 2, 2 duplicates 1) back to the surviving story.
        seen = {story_id}
        while original_id in duplicates and original_id not in seen:
            seen.add(original_id)
            original_id = duplicates[original_id]
        if original_id == story_id:
            continue
        duplicates[story_id] = original_id
        print(f"[duplicates] story {story_id} repeats "
              f"{'sent' if kind == 'sent' else 'candidate'} story {original_id}: {text[:70]!r}")

    return duplicates


def mark_duplicates(cur, duplicates):
    """Records the drop so the same row can't come back next run under its own steam.

    Sources are deliberately left where they are. Moving them onto the survivor would
    rewrite a story's coverage breakdown -- the thing the whole bias layer exists to
    report honestly -- on the strength of one model call.
    """
    for duplicate_id, original_id in duplicates.items():
        cur.execute(
            "UPDATE stories SET status = 'duplicate', duplicate_of = ? WHERE id = ?",
            (original_id, duplicate_id),
        )
    return len(duplicates)
