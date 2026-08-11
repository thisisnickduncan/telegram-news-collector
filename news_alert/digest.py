"""Digest composition: turn a run's eligible stories into Telegram messages.

Returns a LIST of messages, not one. Telegram attaches an inline keyboard to the
bottom of a whole message -- there is no way to put buttons between two blocks of text
in a single message -- so "Follow/Ask directly under each story" necessarily means one
message per story. That's also what lets the numbering go away: a button that lives
under its own story doesn't need "#3" to say which story it belongs to.

Message order: header, then one per story, then the follow-up prompt.
No telegram_client calls here -- this just builds the payloads; pipeline.py sends them.
"""
import html
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news_alert.bias import LEAN_BUCKET, story_coverage
from news_alert.config import DISPLAY_TIMEZONE
from news_alert.sources import independent_sources

MAX_SOURCE_LINKS = 4

# Only these become <a href> targets. GDELT supplies the urls, so without this the
# thing we point anchor tags at is decided by an upstream API we don't control.
# Telegram happens not to linkify javascript:/data: hrefs, which makes this
# defense-in-depth rather than a live hole -- but "our HTML is safe because the
# renderer is picky" is a property of Telegram, not of this code.
SAFE_URL_SCHEMES = ("http://", "https://")


def _safe_url(url):
    """The url to link to, or None if it isn't a plain web address.

    Leading whitespace is stripped before the scheme test: HTML tolerates (and ignores)
    it inside an attribute, so " javascript:..." would pass a naive startswith check and
    still resolve as a script url.
    """
    if not isinstance(url, str):
        return None
    cleaned = url.strip()
    return cleaned if cleaned.lower().startswith(SAFE_URL_SCHEMES) else None

FOLLOW_UP_PROMPT = (
    "\U0001F5E3 Let me know if there is a particular story you would like for me to "
    "take a look at for the next digest."
)

# Drawn under the digest header. Telegram has no rule element and no heading level, so a
# run of box-drawing characters is the only separator available; kept short enough not to
# wrap on a narrow phone.
DIVIDER = "━" * 18

# Subject chips. GDELT search terms are free text, so most won't match anything here and
# fall back to the neutral pin -- that's fine. The point is that recurring subjects get a
# consistent glyph, not that every subject gets a clever one.
TOPIC_ICONS = (
    (("war", "military", "strike", "missile", "troops", "conflict", "invasion"), "\U0001F6E1"),
    (("cyber", "hack", "ransomware", "breach", "malware"), "\U0001F510"),
    (("election", "senate", "congress", "parliament", "politic", "vote", "campaign"), "\U0001F5F3"),
    (("court", "lawsuit", "trial", "ruling", "judge", "legal", "indict"), "\U00002696"),
    (("ai ", "artificial intelligence", "tech", "software", "chip", "semiconductor"), "\U0001F916"),
    (("econom", "market", "inflation", "fed ", "rate", "trade", "tariff", "business"), "\U0001F4C8"),
    (("climate", "weather", "storm", "hurricane", "wildfire", "flood", "quake"), "\U0001F326"),
    (("health", "disease", "outbreak", "vaccine", "hospital", "drug"), "\U0001FA7A"),
    (("space", "nasa", "rocket", "satellite"), "\U0001F680"),
)
DEFAULT_TOPIC_ICON = "\U0001F4CC"


def _topic_icon(topic):
    text = f" {(topic or '').lower()} "
    for keywords, icon in TOPIC_ICONS:
        if any(keyword in text for keyword in keywords):
            return icon
    return DEFAULT_TOPIC_ICON


def story_keyboard(story_id, following=False, muted=False):
    """The standard buttons under a story.

    Shared with callback_handler rather than duplicated there: editMessageReplyMarkup
    replaces the entire markup, so a button press has to rebuild the whole keyboard, and
    if the two sides disagree about what "normal" looks like a press silently drops a
    button that never comes back.

    Three buttons fit one row only while the first is short. Once followed it reads
    "Following ✓ (tap to stop)", which pushes the row past a phone's width, so it gets a
    row of its own.
    """
    if muted:
        return {"inline_keyboard": [[
            {"text": "Ignored ✓ (tap to undo)", "callback_data": f"unmute:{story_id}"},
        ]]}

    ask = {"text": "Ask", "callback_data": f"ask:{story_id}"}
    ignore = {"text": "Ignore", "callback_data": f"ignore:{story_id}"}

    if following:
        return {"inline_keyboard": [
            [{"text": "Following ✓ (tap to stop)", "callback_data": f"stop:{story_id}"}],
            [ask, ignore],
        ]}
    return {"inline_keyboard": [[
        {"text": "Follow", "callback_data": f"follow:{story_id}"},
        ask,
        ignore,
    ]]}


def ignore_choice_keyboard(story_id):
    """What "Ignore" expands into. Muting a category is worth one deliberate extra tap:
    its effect is invisible until a later digest quietly drops something."""
    return {"inline_keyboard": [
        [{"text": "Just this one", "callback_data": f"mute1:{story_id}"},
         {"text": "Stories like this", "callback_data": f"mutetype:{story_id}"}],
        [{"text": "Cancel", "callback_data": f"keep:{story_id}"}],
    ]}


def why_keyboard(rule_id):
    """Offered alongside the "why?" question after muting a category.

    The question is worth asking -- the reason is what separates "no more lottery" from
    "no more gambling of any kind" -- but it must never be a gate. The rule is already
    live by the time this appears, so skipping costs the user nothing.
    """
    return {"inline_keyboard": [[
        {"text": "Skip", "callback_data": f"whyskip:{rule_id}"},
    ]]}


def _format_time(now):
    hour = now.hour % 12 or 12
    ampm = "am" if now.hour < 12 else "pm"
    return f"{hour}:{now.strftime('%M')}{ampm}"


def _format_date(now):
    # Not %-d/%#d: the format differs between glibc and Windows and this renders on both.
    return f"{now:%a} {now.day} {now:%b}"


def _escape(text):
    # quote=False: Telegram's HTML mode only requires escaping & < > -- escaping quotes
    # too (html.escape's default) renders a literal "&#x27;" for every apostrophe.
    return html.escape(str(text or ""), quote=False)


def _pick_sources(rows):
    """Up to MAX_SOURCE_LINKS links for one story, one per independent report.

    Syndicated copies collapse to a single entry first, so four links are four different
    newsrooms rather than four affiliates of one. Then picks for lean diversity -- one
    from each of Left/Center/Right before taking a second from any side -- so the links
    show you the spread rather than four flavors of the same take. Unrated outlets are
    eligible here (they just don't vote on the coverage math) and fill remaining slots.
    """
    linkable = [row for row in rows if _safe_url(row["url"]) and row["domain"]]
    reports = independent_sources(linkable)
    total = len(reports)

    picked, used_buckets, taken = [], set(), set()

    for i, row in enumerate(reports):
        if len(picked) >= MAX_SOURCE_LINKS:
            break
        bucket = LEAN_BUCKET.get(row["bias_rating"])
        if bucket and bucket not in used_buckets:
            used_buckets.add(bucket)
            picked.append(row)
            taken.add(i)

    for i, row in enumerate(reports):
        if len(picked) >= MAX_SOURCE_LINKS:
            break
        if i not in taken:
            picked.append(row)

    return picked, total


def _source_links(cur, story_id):
    """(rendered links line, number of independent voices behind the story)."""
    cur.execute(
        """SELECT title, url, domain, outlet_name, bias_rating FROM story_sources
           WHERE story_id = ? AND url IS NOT NULL""",
        (story_id,),
    )
    picked, total = _pick_sources(cur.fetchall())
    if not picked:
        return "", total

    links = []
    for row in picked:
        label = row["outlet_name"] or row["domain"]
        href = html.escape(_safe_url(row["url"]), quote=True)
        links.append(f'<a href="{href}">{_escape(label)}</a>')

    line = " · ".join(links)
    if total > len(picked):
        line += f" +{total - len(picked)} more"
    return line, total


def _lean_phrase(lean):
    # "Leans Right" -> "leans Right". The label is a hedge, not a heading, and starting it
    # with a capital gives it the weight of a verdict it doesn't have.
    return lean[0].lower() + lean[1:] if lean.startswith("Leans") else lean


def _coverage_line(cur, story_id):
    """Bar + overall lean on one line, or "" when too few sources are rated to say.

    Deliberately renders nothing rather than an empty bar or the word "Unrated" --
    an unrated story just doesn't get a coverage line.
    """
    coverage = story_coverage(cur, story_id)
    if coverage is None:
        return ""
    return f"{coverage['bar']}  {_escape(_lean_phrase(coverage['lean']))}"


def _subject_chip(topic, voices):
    """The small line above a story: what it's filed under, and how many newsrooms."""
    parts = [f"{_topic_icon(topic)} <b>{_escape(topic or 'News')}</b>"]
    if voices >= 2:
        parts.append(f"{voices} voices")
    return "  ·  ".join(parts)


def _story_message(cur, story_id, uncorroborated=False):
    cur.execute(
        "SELECT headline, summary, status, topic FROM stories WHERE id = ?",
        (story_id,),
    )
    story = cur.fetchone()
    if story is None:
        return None

    # Prefer the Claude-written summary over the raw (often messily-scraped) headline.
    body = story["summary"] or story["headline"]
    links, voices = _source_links(cur, story_id)

    # Blockquote rather than bold: Telegram draws it with its own indent and left rule, so
    # the story text reads as a block of prose instead of a wall of shouted headline, and
    # the eye can find where one story stops without counting blank lines.
    parts = [
        _subject_chip(story["topic"], voices),
        f"<blockquote>{_escape(body)}</blockquote>",
    ]

    # Sources and sourcing sit together on consecutive lines -- they're one statement
    # about provenance, and separating them cost a blank line for nothing.
    footer = [links] if links else []
    if uncorroborated:
        # Sits where the coverage bar would go, because it's the same statement: this is
        # what we know about how well-sourced the story is. Never dressed up as a spread.
        footer.append("<i>Single source — not yet corroborated.</i>")
    else:
        coverage = _coverage_line(cur, story_id)
        if coverage:
            footer.append(coverage)
    if footer:
        parts.append("\n".join(footer))

    return {
        "text": "\n\n".join(parts),
        "reply_markup": story_keyboard(story_id, following=story["status"] == "followed"),
        "story_id": story_id,
    }


def deep_dive_message(cur, story_id):
    """The expanded briefing for a story you asked to go deeper on.

    Sent on its own, the moment you ask, rather than folded into the next digest. The
    answer used to wait for the following cycle, which meant asking a question at 8:05
    and reading the answer at noon, next to nine stories you hadn't asked about.
    """
    cur.execute(
        "SELECT expanded_summary, status FROM stories WHERE id = ?", (story_id,)
    )
    story = cur.fetchone()
    if story is None or not story["expanded_summary"]:
        return None

    parts = ["\U0001F50E <b>Deeper coverage</b>",
             f"<blockquote>{_escape(story['expanded_summary'])}</blockquote>"]
    links, _ = _source_links(cur, story_id)
    footer = [links] if links else []
    coverage = _coverage_line(cur, story_id)
    if coverage:
        footer.append(coverage)
    if footer:
        parts.append("\n".join(footer))

    return {
        "text": "\n\n".join(parts),
        "reply_markup": story_keyboard(story_id, following=story["status"] == "followed"),
        "story_id": story_id,
    }


# How much of the earlier summary is echoed under an update. Enough to place the story
# without re-reading it; short enough that the new fact is what the eye lands on.
EARLIER_CHARS = 130


def _update_message(cur, story_id, development=None):
    """A story you've already been sent, coming back because something happened.

    `development` is the one-sentence new fact from developments.py. Without it there is
    nothing to say that wasn't said last time, so the message renders the old summary and
    the caller shouldn't have asked -- but it stays honest either way by labelling what
    the reader was told before.
    """
    cur.execute("SELECT headline, summary, status, topic FROM stories WHERE id = ?",
                (story_id,))
    row = cur.fetchone()
    if row is None:
        return None

    told = (row["summary"] or row["headline"] or "").strip()
    blurb = (development or told or "new development").strip()

    parts = [f"↻ <b>Update</b>  ·  {_escape(row['topic'] or 'News')}",
             f"<blockquote>{_escape(blurb)}</blockquote>"]

    if development and told:
        earlier = told if len(told) <= EARLIER_CHARS else told[:EARLIER_CHARS].rstrip() + "…"
        parts.append(f"<i>Earlier: {_escape(earlier)}</i>")

    links, _ = _source_links(cur, story_id)
    footer = [links] if links else []
    coverage = _coverage_line(cur, story_id)
    if coverage:
        footer.append(coverage)
    if footer:
        parts.append("\n".join(footer))

    return {
        "text": "\n\n".join(parts),
        "reply_markup": story_keyboard(story_id, following=row["status"] == "followed"),
        "story_id": story_id,
    }


def compose_digest(cur, story_ids, update_ids=(), now=None,
                   uncorroborated_ids=(), developments=None):
    """Builds {"messages": [...], "story_ids": [...]} or None if there's nothing to send.

    story_ids: ordered story ids to show as the body of the digest. The caller decides
    eligibility and ordering (see pipeline.py -- it enforces the 2-source minimum and
    ranks by topicality); this function just renders what it's handed. Requested
    deep-dives are not part of a digest at all any more -- they're sent when asked for,
    by deep_dive_message().

    Each returned message is {text, reply_markup, story_id?, disable_notification}.
    Only the header pings the phone; the rest arrive silently so one digest is one
    notification rather than ten.
    """
    story_ids = list(story_ids)
    update_ids = [sid for sid in update_ids if sid not in set(story_ids)]
    if not story_ids and not update_ids:
        return None

    uncorroborated = set(uncorroborated_ids)
    developments = developments or {}

    # Must be timezone-aware: the server runs on UTC, so a naive now() would label the
    # 8:00pm Pacific digest "3:00am".
    now = now or datetime.now(ZoneInfo(DISPLAY_TIMEZONE))

    messages = [{"text": f"\U0001F4F0  <b>News</b>  ·  {_format_date(now)}  ·  "
                         f"{_format_time(now)}\n{DIVIDER}",
                 "reply_markup": None}]

    shown = []
    for story_id in story_ids:
        message = _story_message(cur, story_id,
                                 uncorroborated=story_id in uncorroborated)
        if message is not None:
            messages.append(message)
            shown.append(story_id)

    for story_id in update_ids:
        message = _update_message(cur, story_id, development=developments.get(story_id))
        if message is not None:
            messages.append(message)
            shown.append(story_id)

    if len(messages) == 1:
        return None

    # No "+N more this cycle" line. The stories that don't fit cleared exactly the same
    # bar as the ones that did, so the count invited you to go looking for something you
    # can't reach, and its advice -- narrow your topics -- was wrong: the binding
    # constraint is the per-digest cap, not the breadth of the search. The number is
    # still logged for tuning that cap.
    messages.append({"text": FOLLOW_UP_PROMPT, "reply_markup": None})

    for i, message in enumerate(messages):
        message["disable_notification"] = i > 0

    return {"messages": messages, "story_ids": shown}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from news_alert.db import db_cursor

    with db_cursor() as cur:
        cur.execute(
            """SELECT story_id FROM story_sources GROUP BY story_id
               HAVING COUNT(DISTINCT domain) >= 2 LIMIT 6"""
        )
        ids = [r["story_id"] for r in cur.fetchall()]
        digest = compose_digest(cur, ids)

    if digest is None:
        print("Nothing to send.")
    else:
        for message in digest["messages"]:
            print("-" * 50)
            print(message["text"])
            if message.get("reply_markup"):
                print(f"  [buttons] {message['reply_markup']}")
