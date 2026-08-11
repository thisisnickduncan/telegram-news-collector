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
    "they wanted and they never find out.\n\n"
    "If the user says WHY they don't want it, that reason -- not the story -- is what "
    "you are naming. The story is one example of what they're tired of; their words say "
    "what the boundary actually is. \"I don't care about gambling\" from a Powerball "
    "story means \"gambling and betting\", not \"lottery jackpots\". \"Too depressing\" "
    "from a shooting story means violent crime coverage, not that one city. Where the "
    "reason contradicts the obvious reading of the story, follow the reason."
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
    "existed, so when a headline is ambiguous, leave it out.\n\n"
    "Some rules come with the user's own words about why they muted it, shown as "
    "(reason: ...). Those words are the boundary -- they say what the user is actually "
    "avoiding, which the category name can only approximate. Use them to settle the "
    "ambiguous cases in both directions: a headline the reason plainly covers is a "
    "match even if the category name is a loose fit, and one the reason plainly does "
    "not cover is not a match even if the name would catch it."
)

_MATCH_RE = re.compile(r"^\s*(\d+)\s*[:.\)-]\s*R(\d+)\s*$", re.IGNORECASE)


def _client(client=None):
    return client or anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def describe_rule(headline, summary=None, client=None, reason=None):
    """Turns the story you were looking at into a reusable category. None if it fails.

    `reason` is the user's own answer to "why?", asked after the rule is already in
    force. It is the difference between a category guessed from one example and one the
    user actually stated -- the same Powerball story means "lottery jackpots" to someone
    bored of lotteries and "gambling and betting" to someone avoiding gambling, and
    nothing in the headline distinguishes those.
    """
    body = (summary or headline or "").strip()
    if not body:
        return None

    content = f"Story: {body}"
    if reason:
        content += f"\n\nUser's reason for muting it: {reason.strip()}"

    response = _client(client).messages.create(
        model=MODEL,
        max_tokens=40,
        system=RULE_SYSTEM,
        messages=[{"role": "user", "content": content}],
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


def add_rule(cur, rule, story_id=None, headline=None, reason=None):
    """Stores a rule, or returns the existing id if that category is already muted."""
    cur.execute("SELECT id FROM mute_rules WHERE active = 1 AND LOWER(rule) = ?", (rule.lower(),))
    existing = cur.fetchone()
    if existing:
        return existing["id"], False

    cur.execute(
        """INSERT INTO mute_rules
           (rule, source_story_id, source_headline, reason, created_at, active)
           VALUES (?, ?, ?, ?, ?, 1)""",
        (rule, story_id, headline, reason, datetime.now(timezone.utc).isoformat()),
    )
    return cur.lastrowid, True


def refine_rule(cur, rule_id, client=None, reason=None):
    """Re-derives a rule now that the user has said why, and stores the reason.

    Returns (old_rule, new_rule) -- equal when the reason didn't change the category,
    which is the common case and is not a failure. Returns None if the rule is gone.

    The rule is rewritten rather than added alongside: the reason is a correction to the
    guess made from one headline, and keeping both would leave the original over- or
    under-broad rule quietly filtering as well.
    """
    cur.execute(
        "SELECT rule, source_story_id, source_headline FROM mute_rules "
        "WHERE id = ? AND active = 1",
        (rule_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None

    old_rule = row["rule"]
    refined = describe_rule(row["source_headline"], client=client, reason=reason)
    new_rule = refined or old_rule

    # A refinement that collides with a rule the user already has would create a
    # duplicate category; keep the reason on this row and leave the text alone.
    if new_rule.lower() != old_rule.lower():
        cur.execute(
            "SELECT id FROM mute_rules WHERE active = 1 AND LOWER(rule) = ? AND id != ?",
            (new_rule.lower(), rule_id),
        )
        if cur.fetchone():
            new_rule = old_rule

    cur.execute("UPDATE mute_rules SET rule = ?, reason = ? WHERE id = ?",
                (new_rule, reason, rule_id))
    if new_rule != old_rule:
        print(f"[mutes] rule {rule_id} refined by reason {reason!r}: "
              f"{old_rule!r} -> {new_rule!r}")
    return old_rule, new_rule


def active_rules(cur):
    cur.execute("SELECT id, rule, reason, source_headline, created_at "
                "FROM mute_rules WHERE active = 1 ORDER BY id")
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

    rule_lines = "\n".join(
        f"R{i}: {row['rule']}" + (f"  (reason: {row['reason']})" if row["reason"] else "")
        for i, row in enumerate(rules, start=1)
    )
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
