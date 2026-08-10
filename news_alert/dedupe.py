"""Story clustering: match freshly-fetched articles against active stories.

Intentionally simple fuzzy match (rapidfuzz token_sort_ratio) scoped to same-topic
stories, not embeddings or a real clustering pipeline -- fine at personal scale
(dozens of stories per cycle). See plan section 5.2 / 11.
"""
import hashlib
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from rapidfuzz import fuzz

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news_alert.db import db_cursor

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


def dedupe_articles(articles):
    """Runs each article through match-or-create against the stories table.

    Returns a list of action dicts: {action: 'new'|'matched'|'update', story_id,
    headline, domain, score}. 'update' means the matched story is currently followed --
    digest.py uses this to surface it as an update rather than a new item, and attaches
    a Follow button (callback_data=f"follow:{story_id}") for 'new' stories.
    """
    results = []
    now = datetime.now(timezone.utc).isoformat()

    with db_cursor() as cur:
        for article in articles:
            title = article.get("title")
            if not title:
                continue
            normalized = normalize_title(title)
            topic = article["topic"]

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
