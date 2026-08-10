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
    "\U0001F5E3 Which of these do you want deeper coverage on next time?\n"
    "Just reply naming them however you like -- \"the Iran one and the cyber story\" is "
    "fine. No rush: whenever you get to it, I'll pick it up and lead the next digest "
    "with them."
)


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


def _format_time(now):
    hour = now.hour % 12 or 12
    ampm = "am" if now.hour < 12 else "pm"
    return f"{hour}:{now.strftime('%M')}{ampm}"


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
    cur.execute(
        """SELECT title, url, domain, outlet_name, bias_rating FROM story_sources
           WHERE story_id = ? AND url IS NOT NULL""",
        (story_id,),
    )
    picked, total = _pick_sources(cur.fetchall())
    if not picked:
        return ""

    links = []
    for row in picked:
        label = row["outlet_name"] or row["domain"]
        href = html.escape(_safe_url(row["url"]), quote=True)
        links.append(f'<a href="{href}">{_escape(label)}</a>')

    line = " · ".join(links)
    if total > len(picked):
        line += f" +{total - len(picked)} more"
    return line


def _coverage_block(cur, story_id):
    """Bar + counts + overall lean, or "" when no source on the story is rated.

    Deliberately renders nothing rather than an empty bar or the word "Unrated" --
    an unrated story just doesn't get a coverage line.
    """
    coverage = story_coverage(cur, story_id)
    if coverage is None:
        return ""
    return (f"{coverage['bar']} <b>{_escape(coverage['lean'])}</b>\n"
            f"Coverage: {_escape(coverage['summary_line'])}")


def _story_message(cur, story_id, deep_dive=False, uncorroborated=False, graduated=False):
    cur.execute(
        "SELECT headline, summary, expanded_summary, status FROM stories WHERE id = ?",
        (story_id,),
    )
    story = cur.fetchone()
    if story is None:
        return None

    if deep_dive and story["expanded_summary"]:
        body = story["expanded_summary"]
    else:
        # Prefer the Claude-written summary over the raw (often messily-scraped) headline.
        body = story["summary"] or story["headline"]

    parts = []
    if deep_dive:
        parts.append("\U0001F50E <b>Deeper coverage</b>")
    elif graduated:
        # This one was shown before as a single source; other outlets have since picked
        # it up. Saying so is why seeing it twice isn't confusing.
        parts.append("\U0001F53A <b>Now corroborated</b>")
    parts.append(f"<b>{_escape(body)}</b>" if not deep_dive else _escape(body))

    links = _source_links(cur, story_id)
    if links:
        parts.append(links)

    if uncorroborated:
        # Sits where the coverage bar would go, because it's the same statement: this is
        # what we know about how well-sourced the story is. Never dressed up as a spread.
        parts.append("<i>Single source — not yet corroborated.</i>")
    else:
        coverage = _coverage_block(cur, story_id)
        if coverage:
            parts.append(coverage)

    return {
        "text": "\n\n".join(parts),
        "reply_markup": story_keyboard(story_id, following=story["status"] == "followed"),
        "story_id": story_id,
    }


def _update_message(cur, story_id):
    cur.execute("SELECT headline, summary FROM stories WHERE id = ?", (story_id,))
    row = cur.fetchone()
    if row is None:
        return None
    headline = (row["headline"] or "").strip()
    blurb = row["summary"] or "new development"

    parts = [f'↻ <b>Update</b> — {_escape(headline)}', _escape(blurb)]
    links = _source_links(cur, story_id)
    if links:
        parts.append(links)
    coverage = _coverage_block(cur, story_id)
    if coverage:
        parts.append(coverage)

    # An update only exists for a story you follow, so the keyboard is always the
    # followed variant.
    return {
        "text": "\n\n".join(parts),
        "reply_markup": story_keyboard(story_id, following=True),
        "story_id": story_id,
    }


def compose_digest(cur, story_ids, update_ids=(), deep_dive_ids=(), now=None,
                   uncorroborated_ids=(), graduated_ids=()):
    """Builds {"messages": [...], "story_ids": [...]} or None if there's nothing to send.

    story_ids: ordered story ids to show as the body of the digest. The caller decides
    eligibility and ordering (see pipeline.py -- it enforces the 2-source minimum and
    puts requested deep-dives first); this function just renders what it's handed.
    deep_dive_ids: subset of story_ids to render with their expanded write-up.

    Each returned message is {text, reply_markup, story_id?, disable_notification}.
    Only the header pings the phone; the rest arrive silently so one digest is one
    notification rather than ten.
    """
    story_ids = list(story_ids)
    update_ids = [sid for sid in update_ids if sid not in set(story_ids)]
    if not story_ids and not update_ids:
        return None

    deep_dive = set(deep_dive_ids)
    uncorroborated = set(uncorroborated_ids)
    graduated = set(graduated_ids)

    # Must be timezone-aware: the server runs on UTC, so a naive now() would label the
    # 8:00pm Pacific digest "3:00am".
    now = now or datetime.now(ZoneInfo(DISPLAY_TIMEZONE))

    messages = [{"text": f"\U0001F4F0 <b>News — {_format_time(now)}</b>",
                 "reply_markup": None}]

    shown = []
    for story_id in story_ids:
        message = _story_message(cur, story_id, deep_dive=story_id in deep_dive,
                                 uncorroborated=story_id in uncorroborated,
                                 graduated=story_id in graduated)
        if message is not None:
            messages.append(message)
            shown.append(story_id)

    for story_id in update_ids:
        message = _update_message(cur, story_id)
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
