"""Story clustering: match freshly-fetched articles against active stories.

Started as a fuzzy match alone (rapidfuzz token_sort_ratio, threshold 80, scoped to
same-topic stories). That turned out to cluster only *republished copy*, never genuine
corroboration: measured on a live run, three newsrooms reporting one Iranian appointment
scored 52-57 against each other, so they became three separate single-source stories,
while a wire piece syndicated to nine Hearst affiliates scored ~100 and clustered
perfectly. Since a story needs two independent sources to reach the digest, that meant
the digest could only ever show syndication.

So semantic grouping (events.py) runs first and fuzzy matching is now the fallback
underneath it. The fuzzy layer still earns its place -- it catches syndicated reprints
for free and keeps the pipeline working if the grouping call fails.
"""
import hashlib
import re
import sys
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from rapidfuzz import fuzz

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news_alert.db import db_cursor
from news_alert.events import assign_events

MATCH_THRESHOLD = 80


def normalize_title(title):
    title = (title or "").lower()
    title = re.sub(r"[^\w\s]", "", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title


def _cluster_key(topic, normalized_title):
    # Not used for matching (that's the fuzzy compare against `headline`) -- just needs
    # to be unique per row. A pure hash of (topic, title) would collide if the same
    # headline legitimately recurs later (story expires and resurfaces, or two unrelated
    # events share wording), so tag it with a random suffix.
    raw = f"{topic}::{normalized_title}::{uuid.uuid4().hex[:8]}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _find_match(cur, topic, normalized_title):
    """Returns (story_row, score) for the best same-topic match above threshold, or (None, 0)."""
    cur.execute("SELECT * FROM stories WHERE status != 'expired' AND topic = ?", (topic,))
    best_row, best_score = None, 0
    for row in cur.fetchall():
        score = fuzz.token_sort_ratio(normalized_title, normalize_title(row["headline"]))
        if score > best_score:
            best_row, best_score = row, score
    if best_score >= MATCH_THRESHOLD:
        return best_row, best_score
    return None, 0


def dedupe_articles(articles, client=None, group_events=True):
    """Runs each article through match-or-create against the stories table.

    Returns a list of action dicts: {action: 'new'|'matched'|'update', story_id,
    headline, domain, score}. 'update' means the matched story is currently followed --
    digest.py uses this to surface it as an update rather than a new item, and attaches
    a Follow button (callback_data=f"follow:{story_id}") for 'new' stories.

    Two layers decide what clusters with what:

      1. events.assign_events() reads the run's headlines and says which describe the
         same real-world occurrence. This is the layer that finds corroboration --
         fuzzy matching structurally cannot, because different newsrooms writing about
         one event score 44-67 against each other while the threshold is 80.
      2. Anything the grouper didn't place falls through to the original per-article
         fuzzy match, which still catches syndicated reprints (they score ~100).

    group_events=False forces layer 2 only -- useful for offline tests that shouldn't
    call the API. `score` is None for semantically grouped articles; there's no
    similarity number involved.
    """
    results = []
    now = datetime.now(timezone.utc).isoformat()

    with db_cursor() as cur:
        if group_events and articles:
            try:
                assignments = assign_events(cur, articles, client=client)
            except Exception:
                print("[dedupe] event grouping unavailable, falling back to fuzzy matching:")
                traceback.print_exc()
                assignments = [None] * len(articles)
        else:
            assignments = [None] * len(articles)

        # New events created during this run, so the second and third article about one
        # occurrence attach to the story the first one created.
        group_story_ids = {}

        for article, assignment in zip(articles, assignments):
            title = article.get("title")
            if not title:
                continue
            normalized = normalize_title(title)
            topic = article["topic"]

            match, score = None, None
            if isinstance(assignment, int):
                cur.execute("SELECT * FROM stories WHERE id = ?", (assignment,))
                match = cur.fetchone()
            elif isinstance(assignment, str):
                existing_id = group_story_ids.get(assignment)
                if existing_id is not None:
                    cur.execute("SELECT * FROM stories WHERE id = ?", (existing_id,))
                    match = cur.fetchone()

            # Ungrouped, or the grouper named a story id that no longer exists.
            if match is None and not isinstance(assignment, str):
                match, score = _find_match(cur, topic, normalized)

            if match is not None:
                story_id = match["id"]
                cur.execute(
                    "UPDATE stories SET last_updated_at = ? WHERE id = ?",
                    (now, story_id),
                )
                cur.execute(
                    """INSERT INTO story_sources
                       (story_id, title, url, domain, outlet_name, bias_rating, published_at, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (story_id, title, article.get("url"), article.get("domain"), None, None,
                     article.get("seendate"), now),
                )
                action = "update" if match["status"] == "followed" else "matched"
                results.append({
                    "action": action,
                    "story_id": story_id,
                    "headline": match["headline"],
                    "domain": article.get("domain"),
                    "score": score,
                })
            else:
                cluster_key = _cluster_key(topic, normalized)
                cur.execute(
                    """INSERT INTO stories
                       (cluster_key, headline, summary, region, topic,
                        status, first_seen_at, last_updated_at)
                       VALUES (?, ?, NULL, ?, ?, 'active', ?, ?)""",
                    (cluster_key, title, article.get("region"), topic, now, now),
                )
                story_id = cur.lastrowid
                if isinstance(assignment, str):
                    group_story_ids[assignment] = story_id
                cur.execute(
                    """INSERT INTO story_sources
                       (story_id, title, url, domain, outlet_name, bias_rating, published_at, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (story_id, title, article.get("url"), article.get("domain"), None, None,
                     article.get("seendate"), now),
                )
                results.append({
                    "action": "new",
                    "story_id": story_id,
                    "headline": title,
                    "domain": article.get("domain"),
                    "score": None,
                })

    return results


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from news_alert.db import get_preferences
    from news_alert.fetcher import fetch_all

    prefs = get_preferences()
    if prefs is None:
        print("No preferences set. Run scripts/set_preferences.py first.")
        sys.exit(1)

    articles = fetch_all(prefs)
    print(f"\nFetched {len(articles)} articles, running dedupe...\n")

    actions = dedupe_articles(articles)

    new_count = sum(1 for a in actions if a["action"] == "new")
    matched_count = sum(1 for a in actions if a["action"] == "matched")
    update_count = sum(1 for a in actions if a["action"] == "update")

    for a in actions:
        tag = {"new": "NEW", "matched": "DUP", "update": "UPD"}[a["action"]]
        score = f" ({a['score']}%)" if a["score"] is not None else ""
        print(f"  [{tag}] id={a['story_id']}{score}  {a['headline']}  <- {a['domain']}")

    print(f"\n{new_count} new stories, {matched_count} additional sources on existing stories, "
          f"{update_count} updates to followed stories.")
