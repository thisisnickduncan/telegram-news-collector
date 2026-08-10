"""The end-of-digest follow-up loop.

Every digest ends by asking which stories you want more on. Your answer may come back
in ten seconds or three hours -- the queue is what makes that difference not matter.
A reply is matched against the *last digest sent*, which stays the active target until
the next one goes out, so a late reply still lands on the right stories and is picked
up by the following run.

Requests are consumed (not deleted) by the run that acts on them, so a story you asked
about leads exactly one digest rather than every digest forever.
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


def queue_stories(cur, story_ids):
    """Queues stories for expanded coverage. Ignores anything already pending, so
    asking twice doesn't double up."""
    queued = []
    now = datetime.now(timezone.utc).isoformat()
    for story_id in story_ids:
        cur.execute(
            "SELECT 1 FROM deep_dive_requests WHERE story_id = ? AND consumed_at IS NULL",
            (story_id,),
        )
        if cur.fetchone() is not None:
            continue
        cur.execute(
            "INSERT INTO deep_dive_requests (story_id, requested_at, consumed_at) VALUES (?, ?, NULL)",
            (story_id, now),
        )
        queued.append(story_id)
    return queued


def pending_story_ids(cur):
    """Unconsumed requests, oldest first -- the stories the next digest should lead with."""
    cur.execute(
        """SELECT story_id FROM deep_dive_requests WHERE consumed_at IS NULL
           GROUP BY story_id ORDER BY MIN(requested_at)"""
    )
    return [row["story_id"] for row in cur.fetchall()]


def consume(cur, story_ids):
    """Marks requests handled. Called after the digest that acted on them is sent, so a
    run that dies mid-flight leaves the request queued for the next one."""
    if not story_ids:
        return 0
    placeholders = ",".join("?" * len(story_ids))
    cur.execute(
        f"""UPDATE deep_dive_requests SET consumed_at = ?
            WHERE consumed_at IS NULL AND story_id IN ({placeholders})""",
        (datetime.now(timezone.utc).isoformat(), *story_ids),
    )
    return cur.rowcount
