"""Orchestration: fetch -> dedupe -> bias tag -> select -> summarize -> compose -> hold -> send.
Runs top to bottom, logs each stage, exits.
"""
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news_alert.bias import tag_untagged_sources
from news_alert.db import get_preferences, db_cursor
from news_alert.dedupe import dedupe_articles
from news_alert.deep_dive import consume, pending_story_ids, record_digest
from news_alert.digest import compose_digest
from news_alert.fetcher import fetch_all
from news_alert.relevance import topic_score
from news_alert.sources import count_independent
from news_alert.summarizer import expand_story, summarize_pending_stories
from news_alert.telegram_client import safe_call, send_message

# A story must be carried by at least this many DISTINCT outlets before it's worth
# sending. One outlet saying something is a claim; two independently reporting it is a
# story. Counting distinct domains, not articles, matters -- a wire piece syndicated
# three times on the same site is still one voice.
MIN_SOURCES = 2

# If fewer than this many stories clear the corroboration bar, the digest is topped up
# with the freshest single-source stories, each marked as such. Measured on live data,
# only ~2.4% of stories reach two genuinely independent reports, so without a floor most
# digests would arrive with one story or none. The marker is what keeps this honest:
# nothing is ever presented as corroborated when it isn't.
DIGEST_MIN_STORIES = 4

# How long an undelivered story stays a candidate. This is what makes the
# hold-for-corroboration rule work: a single-source story isn't dropped, it just waits,
# and if a second outlet picks it up within this window it becomes eligible and ships.
# Past that it's no longer news and is left behind rather than surfacing stale.
STORY_MAX_AGE_HOURS = 24

# Telegram will throttle a burst of messages to one chat. A digest is now one message
# per story, so they're paced out slightly. The header still lands exactly on the hour.
SEND_SPACING_SECONDS = 0.4


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


def _rank_candidates(cur, max_stories, min_sources):
    """Works out what this digest should carry.

    Returns {selected, overflow, held, uncorroborated, graduated} where `selected` is
    already in send order: corroborated stories first, ranked by how many independent
    outlets carried them, then -- only if that came up short of DIGEST_MIN_STORIES --
    the freshest single-source stories as a marked-up floor.

    Eligibility is "not yet delivered AND still fresh", deliberately not "created during
    this run". A story created last cycle with one source isn't new any more, so a
    run-scoped test could never deliver it even after a second outlet corroborated it;
    keying off delivered_at is what lets a held story graduate into a later digest.

    Ranked by topicality first, then by how many outlets carried it, and only then by
    recency. Recency is the weakest of the three: within a 4-hour window everything is
    about equally fresh, so it effectively sorts by primary key -- which once put a
    2-source celebrity item above a 9-source Strait of Hormuz story. Topicality leads
    because GDELT matches a query anywhere in an article's text, so a well-sourced story
    can be well-sourced about something you never asked for (see relevance.py).

    Distinct domains is only a cheap SQL prefilter. It can't tell an independent report
    from a syndicated reprint, and on real data that difference is the whole ballgame:
    the top story by domain count turned out to be one wire piece on nine Hearst
    affiliates. The real test runs in Python, where headlines can be compared.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=STORY_MAX_AGE_HOURS)).isoformat()
    # Already-delivered stories come back only if they were sent as single-source and
    # have since earned real corroboration. COALESCE defaults legacy rows (sent before
    # this column existed) to "fully delivered" so a deploy doesn't resurface history.
    cur.execute(
        """SELECT s.id AS id, s.delivered_at AS delivered_at, s.topic AS topic,
                  COUNT(DISTINCT ss.domain) AS domains, MAX(ss.fetched_at) AS latest
           FROM stories s JOIN story_sources ss ON ss.story_id = s.id
           WHERE s.status != 'expired'
             AND (s.delivered_at IS NULL OR COALESCE(s.delivered_source_count, 99) < ?)
           GROUP BY s.id
           HAVING MAX(ss.fetched_at) >= ?
           ORDER BY latest DESC""",
        (min_sources, cutoff),
    )
    candidates = cur.fetchall()

    strong, weak, graduated = [], [], set()
    for row in candidates:
        if row["domains"] < min_sources:
            # Two articles from one outlet is still one outlet -- no lookup needed.
            voices = 1
            on_topic = 0
        else:
            cur.execute(
                "SELECT title, domain, bias_rating FROM story_sources WHERE story_id = ?",
                (row["id"],),
            )
            sources = cur.fetchall()
            voices = count_independent(sources)
            on_topic = topic_score(row["topic"], [s["title"] for s in sources])

        if voices >= min_sources:
            strong.append((on_topic, voices, row["latest"], row["id"]))
            if row["delivered_at"] is not None:
                graduated.add(row["id"])
        elif row["delivered_at"] is None:
            weak.append((row["latest"], row["id"]))

    # Topicality outranks source count: a story genuinely about what you asked for beats
    # a better-sourced one that merely mentioned it. Ranking rather than filtering --
    # see relevance.py for why gating on this would starve the digest.
    strong.sort(reverse=True)
    weak.sort(reverse=True)
    corroborated = [story_id for _, _, _, story_id in strong]
    held = len(weak)

    selected = corroborated[:max_stories]
    overflow = max(len(corroborated) - max_stories, 0)

    # The floor. A digest of one story four times a day is worse than the thing this
    # replaced, and on measured data genuine corroboration is rare enough that this
    # will fire often. Topping up is honest as long as it's labelled -- these render
    # with a "single source" marker and never contribute a coverage bar.
    uncorroborated = []
    if len(selected) < DIGEST_MIN_STORIES:
        room = min(DIGEST_MIN_STORIES, max_stories) - len(selected)
        uncorroborated = [story_id for _, story_id in weak[:room]]
        selected = selected + uncorroborated

    return {
        "selected": selected,
        "overflow": overflow,
        "held": held - len(uncorroborated),
        "uncorroborated": uncorroborated,
        "graduated": graduated,
    }


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

    with db_cursor() as cur:
        # Stories you asked for more on lead the digest, ahead of anything new.
        deep_dive_ids = pending_story_ids(cur)
        plan = _rank_candidates(cur, prefs["digest_max_stories"], MIN_SOURCES)
        selected, overflow, held = plan["selected"], plan["overflow"], plan["held"]

        # Followed stories that picked up something this run still surface as updates,
        # regardless of the source minimum -- you asked to be told about those.
        update_ids = sorted({r["story_id"] for r in results if r["action"] == "update"})

    ordered = deep_dive_ids + [sid for sid in selected if sid not in set(deep_dive_ids)]
    corroborated = len(selected) - len(plan["uncorroborated"])
    print(f"[pipeline] {corroborated} corroborated ({MIN_SOURCES}+ independent sources), "
          f"{len(plan['uncorroborated'])} single-source fill, {overflow} overflow, "
          f"{held} held awaiting corroboration, {len(plan['graduated'])} newly corroborated, "
          f"{len(deep_dive_ids)} deep-dive requested, {len(update_ids)} followed updates.")

    if deep_dive_ids:
        print(f"[pipeline] Expanding {len(deep_dive_ids)} requested stories...")
        with db_cursor() as cur:
            for story_id in deep_dive_ids:
                try:
                    expand_story(cur, story_id)
                except Exception:
                    # A failed expansion shouldn't sink the digest -- the story still
                    # renders with its normal summary.
                    print(f"[pipeline] expand failed for story {story_id}:")
                    traceback.print_exc()

    print(f"[pipeline] Summarizing {len(ordered)} shown + {len(update_ids)} followed...")
    with db_cursor() as cur:
        summarized = summarize_pending_stories(cur, story_ids=ordered + update_ids)
    print(f"[pipeline] Summarized {summarized} stories.")

    # now=send_at so the header reads the time the digest will actually arrive, not
    # the time it happened to finish composing.
    with db_cursor() as cur:
        digest = compose_digest(cur, ordered, update_ids=update_ids,
                                deep_dive_ids=deep_dive_ids, now=send_at,
                                uncorroborated_ids=plan["uncorroborated"],
                                graduated_ids=plan["graduated"])

    if digest is None:
        print(f"[pipeline] Nothing corroborated by {MIN_SOURCES}+ sources and no followed "
              f"updates ({held} stories held). Not sending.")
        return

    if send_at is not None:
        _hold_until(send_at)

    print(f"[pipeline] Sending digest as {len(digest['messages'])} messages...")
    sent_ids = []
    for i, message in enumerate(digest["messages"]):
        if i:
            time.sleep(SEND_SPACING_SECONDS)
        try:
            sent = send_message(
                prefs["telegram_chat_id"], message["text"],
                reply_markup=message.get("reply_markup"),
                disable_notification=message.get("disable_notification", False),
            )
        except Exception:
            # One story failing to render must not cost you the rest of the digest.
            print(f"[pipeline] send failed for message {i}:")
            traceback.print_exc()
            continue
        story_id = message.get("story_id")
        if story_id is not None:
            sent_ids.append((story_id, str(sent["message_id"])))

    print(f"[pipeline] Sent {len(sent_ids)} story messages.")

    now = datetime.now(timezone.utc).isoformat()
    uncorroborated = set(plan["uncorroborated"])
    with db_cursor() as cur:
        for story_id, message_id in sent_ids:
            # delivered_at keeps its first value -- a deep-dive or newly-corroborated
            # story is being re-shown, and overwriting it would misreport when you first
            # saw it. delivered_source_count DOES advance: that's the record of what the
            # story was worth when sent, and raising it is what stops a story that has
            # now been shown with real corroboration from qualifying to return again.
            cur.execute(
                """UPDATE stories SET telegram_message_id = ?,
                       delivered_at = COALESCE(delivered_at, ?),
                       delivered_source_count = MAX(COALESCE(delivered_source_count, 0), ?)
                   WHERE id = ?""",
                (message_id, now, 1 if story_id in uncorroborated else MIN_SOURCES, story_id),
            )
        record_digest(cur, [story_id for story_id, _ in sent_ids])
        # Consumed only after a successful send, so a run that dies mid-flight leaves
        # the request queued for the next one.
        consume(cur, deep_dive_ids)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    run()
