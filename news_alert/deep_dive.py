"""The end-of-digest follow-up loop.

Every digest ends by asking which stories you want more on. Your answer may come back in
ten seconds or three hours, and this is what makes that difference not matter: a reply is
matched against the *last digest sent*, which stays the active target until the next one
goes out, so a late reply still lands on the right stories.

There is no longer a queue. Requests used to be stored and acted on by the following run,
which meant asking at 8:05 and reading the answer at noon; they are answered on the spot
now (message_handler), which costs the same because it is the same call either way. The
`deep_dive_requests` table is left in place rather than dropped, since removing it buys
nothing and a migration that destroys history to save a few kilobytes is a bad trade.
"""
import json
from datetime import datetime, timezone


def record_digest(cur, story_ids):
    """Logs what a digest contained. This is the set a later reply resolves against."""
    cur.execute(
        "INSERT INTO digests (sent_at, story_ids) VALUES (?, ?)",
        (datetime.now(timezone.utc).isoformat(), json.dumps(list(story_ids))),
    )


def last_digest_stories(cur):
    """The stories from the most recent digest, as [{id, headline, summary, topic}].

    Returns them in the order they were shown, so the model sees the same digest you
    did when it works out which one "the second one about Iran" means.
    """
    cur.execute("SELECT story_ids FROM digests ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    if row is None or not row["story_ids"]:
        return []

    try:
        story_ids = json.loads(row["story_ids"])
    except (TypeError, ValueError):
        return []
    if not story_ids:
        return []

    placeholders = ",".join("?" * len(story_ids))
    cur.execute(
        f"SELECT id, headline, summary, topic FROM stories WHERE id IN ({placeholders})",
        tuple(story_ids),
    )
    by_id = {r["id"]: dict(r) for r in cur.fetchall()}
    return [by_id[sid] for sid in story_ids if sid in by_id]
