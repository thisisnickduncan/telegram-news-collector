"""Muting whole categories of story, not just individual ones.

Tapping "Ignore -> stories like this" on a Powerball story should stop Mega Millions
stories too. Keywords cannot do that: "powerball, jackpot" misses "Mega Millions prize
soars" and fires on "state lottery funding bill passes", which is policy and probably
wanted. So the button asks Claude to name the *category* once, stores that sentence, and
screens later candidates against it.

Two calls, both cheap and both bounded:

  * one when you create a rule -- turning a headline into "lottery jackpots and prize
    drawings";
  * one per run, batched over every candidate at once, in the same shape as events.py.

Critically, a run with no active rules makes NO call at all. Muting is opt-in, so
somebody who never presses the button never pays for the feature.

The screening prompt is deliberately conservative. A false positive here silently
deletes news the user wanted and they never learn it existed, which is far worse than a
muted story slipping through into a digest where they can just mute it again.
"""
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news_alert.config import ANTHROPIC_API_KEY

MODEL = "claude-haiku-4-5"

# Candidates screened per call. The digest considers on the order of 60 corroborated
# stories a run, so this is usually one call.
MAX_STORIES_PER_CALL = 60

# Rules offered per call. Well past what anyone will accumulate; the cap exists so the
# prompt cannot grow without bound.
MAX_RULES = 40

RULE_SYSTEM = (
    "The user is reading a personal news digest and just asked to stop seeing stories "
    "like the one shown. Name the CATEGORY they want muted.\n\n"
    "Reply with a short noun phrase, 2-6 words, lowercase, no punctuation, no "
    "explanation -- for example \"lottery jackpots and prize drawings\", \"celebrity "
    "crime and gossip\", \"college football recruiting\".\n\n"
    "Pitch it at the level of the RECURRING SUBJECT, not this one event. \"powerball "
    "drawing on monday\" is too narrow to ever match again; \"news\" is so broad it "
    "would mute everything. When in doubt err narrow: a rule that is too tight only "
    "means they mute a second time, while one that is too loose silently deletes things "
    "they wanted and they never find out."
)

MATCH_SYSTEM = (
    "You filter a personal news digest. The user has muted some categories of story. "
    "Given those rules and a numbered list of candidate headlines, identify which "
    "headlines clearly belong to a muted category.\n\n"
    "Output one line per MATCH, formatted exactly:\n"
    "<headline number>: R<rule number>\n"
    "If nothing matches, output exactly: NONE\n"
    "No other text, no explanation.\n\n"
    "Only flag a headline when it plainly belongs to the muted category. Being related "
    "to it, or sharing a subject with it, is not enough -- if the user muted \"lottery "
    "jackpots and prize drawings\", a story about lottery funding legislation is NOT a "
    "match. A wrongly filtered story disappears without the user ever knowing it "
    "existed, so when a headline is ambiguous, leave it out."
)

_MATCH_RE = re.compile(r"^\s*(\d+)\s*[:.\)-]\s*R(\d+)\s*$", re.IGNORECASE)


def _client(client=None):
    return client or anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def describe_rule(headline, summary=None, client=None):
    """Turns the story you were looking at into a reusable category. None if it fails."""
    body = (summary or headline or "").strip()
    if not body:
        return None

    response = _client(client).messages.create(
        model=MODEL,
        max_tokens=40,
        system=RULE_SYSTEM,
        messages=[{"role": "user", "content": f"Story: {body}"}],
    )
    rule = next((b.text for b in response.content if b.type == "text"), "").strip()
    rule = rule.strip("\"'.").lower()
    print(f"[mutes] rule from {body[:60]!r} -> {rule!r}")
    # A model that ignores the brief and writes a sentence gets discarded rather than
    # stored: an over-broad rule is the expensive failure here.
    if not rule or len(rule.split()) > 10:
        print(f"[mutes] rejecting unusable rule {rule!r}")
        return None
    return rule


def add_rule(cur, rule, story_id=None, headline=None):
    """Stores a rule, or returns the existing id if that category is already muted."""
    cur.execute("SELECT id FROM mute_rules WHERE active = 1 AND LOWER(rule) = ?", (rule.lower(),))
    existing = cur.fetchone()
    if existing:
        return existing["id"], False

    cur.execute(
        """INSERT INTO mute_rules (rule, source_story_id, source_headline, created_at, active)
           VALUES (?, ?, ?, ?, 1)""",
        (rule, story_id, headline, datetime.now(timezone.utc).isoformat()),
    )
    return cur.lastrowid, True


def active_rules(cur):
    cur.execute("SELECT id, rule, source_headline, created_at FROM mute_rules WHERE active = 1 ORDER BY id")
    return cur.fetchall()


def deactivate_rule(cur, rule_id):
    """Soft delete -- kept so an un-muted category can still be explained later."""
    cur.execute("SELECT rule FROM mute_rules WHERE id = ? AND active = 1", (rule_id,))
    row = cur.fetchone()
    if row is None:
        return None
    cur.execute("UPDATE mute_rules SET active = 0 WHERE id = ?", (rule_id,))
    return row["rule"]


def deactivate_rule_for_story(cur, story_id):
    """Removes the rule that was created from this story, if any.

    Exists so "tap to undo" undoes what the tap actually did. Muting a category from a
    story mutes both the story and the category, so undoing only the story would leave an
    invisible rule still deleting future news -- the user pressed undo and would
    reasonably believe nothing was being filtered.
    """
    cur.execute(
        "SELECT id, rule FROM mute_rules WHERE source_story_id = ? AND active = 1",
        (story_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    cur.execute("UPDATE mute_rules SET active = 0 WHERE id = ?", (row["id"],))
    print(f"[mutes] deactivated rule {row['id']} ({row['rule']!r}) via story {story_id} undo")
    return row["rule"]


def muted_story_ids(cur, candidates, client=None):
    """Which of `candidates` belong to a muted category.

    candidates: iterable of (story_id, text). Returns a set of story ids to drop.

    Returns immediately, without any API call, when there are no rules -- the common
    case for anyone who has never pressed the button.
    """
    candidates = [(sid, text) for sid, text in candidates if text]
    if not candidates:
        return set()

    rules = active_rules(cur)[:MAX_RULES]
    if not rules:
        return set()

    rule_lines = "\n".join(f"R{i}: {row['rule']}" for i, row in enumerate(rules, start=1))
    muted = set()

    for start in range(0, len(candidates), MAX_STORIES_PER_CALL):
        chunk = candidates[start:start + MAX_STORIES_PER_CALL]
        listing = "\n".join(f"{i}. {text}" for i, (_, text) in enumerate(chunk, start=1))

        try:
            response = _client(client).messages.create(
                model=MODEL,
                max_tokens=500,
                system=MATCH_SYSTEM,
                messages=[{"role": "user",
                           "content": f"Muted categories:\n{rule_lines}\n\n"
                                      f"Candidate headlines:\n{listing}"}],
            )
        except Exception as exc:
            # Never let the filter take down a digest. Failing open shows a story that
            # should have been hidden; failing closed would send nothing at all.
            print(f"[mutes] screening call failed, showing everything in this chunk: {exc}")
            continue

        reply = next((b.text for b in response.content if b.type == "text"), "").strip()
        if reply.upper().startswith("NONE"):
            continue

        for line in reply.splitlines():
            match = _MATCH_RE.match(line)
            if not match:
                continue
            index, rule_no = int(match.group(1)), int(match.group(2))
            if 1 <= index <= len(chunk) and 1 <= rule_no <= len(rules):
                story_id, text = chunk[index - 1]
                muted.add(story_id)
                print(f"[mutes] muting story {story_id} under R{rule_no} "
                      f"({rules[rule_no - 1]['rule']!r}): {text[:70]!r}")

    return muted
