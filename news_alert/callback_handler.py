"""Parses Telegram inline-button presses.

follow / stop / ask, plus the ignore flow: `ignore` swaps the keyboard for a choice
between muting this one story and muting its whole category, and `mute1` / `mutetype` /
`keep` resolve that choice.

Keyboards are rebuilt wholesale from digest.story_keyboard rather than patched button by
button. editMessageReplyMarkup replaces the entire markup anyway, so patching only ever
risked the two modules disagreeing about what the normal keyboard looks like and quietly
losing a button that never came back.
"""
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news_alert.digest import ignore_choice_keyboard, story_keyboard, why_keyboard
from news_alert.mutes import add_rule, deactivate_rule_for_story, describe_rule
from news_alert.telegram_client import (
    answer_callback_query,
    edit_message_reply_markup,
    safe_call,
    send_message,
)

CALLBACK_RE = re.compile(
    r"^(follow|stop|ask|ignore|mute1|mutetype|keep|unmute|whyskip):(\d+)$"
)


def _set_keyboard(callback_query, markup):
    message = callback_query["message"]
    safe_call(edit_message_reply_markup, message["chat"]["id"], message["message_id"], markup)


def _restore_keyboard(cur, callback_query, story_id):
    """Puts the normal keyboard back, reflecting whatever state the story is now in."""
    cur.execute("SELECT status FROM stories WHERE id = ?", (story_id,))
    row = cur.fetchone()
    status = row["status"] if row else "active"
    _set_keyboard(callback_query, story_keyboard(story_id, following=status == "followed",
                                                 muted=status == "muted"))


def handle_callback_query(cur, callback_query):
    data = callback_query.get("data", "")
    print(f"[callback_handler] received callback_data={data!r}")
    match = CALLBACK_RE.match(data)
    if not match:
        print(f"[callback_handler] {data!r} didn't match {CALLBACK_RE.pattern} -- ignoring")
        safe_call(answer_callback_query, callback_query["id"], text="Unrecognized button.")
        return

    # The trailing number is a story id for every action but whyskip, where it's the id of
    # the mute rule whose "why?" question is being dismissed.
    action, story_id = match.group(1), int(match.group(2))
    print(f"[callback_handler] action={action!r} id={story_id}")
    now = datetime.now(timezone.utc).isoformat()
    chat_id = callback_query["message"]["chat"]["id"]

    if action == "whyskip":
        cur.execute("UPDATE preferences SET pending_why_rule_id = NULL, "
                    "pending_why_at = NULL WHERE id = 1")
        safe_call(answer_callback_query, callback_query["id"], text="No problem.")
        _set_keyboard(callback_query, {"inline_keyboard": []})
        return

    if action == "follow":
        cur.execute(
            "INSERT INTO follows (story_id, followed_at) VALUES (?, ?) "
            "ON CONFLICT(story_id) DO UPDATE SET followed_at = excluded.followed_at",
            (story_id, now),
        )
        cur.execute("UPDATE stories SET status = 'followed' WHERE id = ?", (story_id,))
        safe_call(answer_callback_query, callback_query["id"],
                  text="Following. You'll get updates on this story.")
        _set_keyboard(callback_query, story_keyboard(story_id, following=True))

    elif action == "stop":
        cur.execute("DELETE FROM follows WHERE story_id = ?", (story_id,))
        cur.execute("UPDATE stories SET status = 'active' WHERE id = ?", (story_id,))
        safe_call(answer_callback_query, callback_query["id"], text="Stopped.")
        _set_keyboard(callback_query, story_keyboard(story_id, following=False))

    elif action == "ask":
        cur.execute("SELECT headline FROM stories WHERE id = ?", (story_id,))
        story = cur.fetchone()
        if story is None:
            safe_call(answer_callback_query, callback_query["id"],
                      text="That story isn't available anymore.")
            return
        cur.execute("UPDATE preferences SET pending_ask_story_id = ? WHERE id = 1", (story_id,))
        safe_call(answer_callback_query, callback_query["id"])
        headline = (story["headline"] or "").strip()
        safe_call(send_message, chat_id, f'What would you like to know about "{headline}"?',
                  parse_mode=None)

    elif action == "ignore":
        # Nothing is muted yet -- this only opens the choice.
        safe_call(answer_callback_query, callback_query["id"])
        _set_keyboard(callback_query, ignore_choice_keyboard(story_id))

    elif action == "keep":
        safe_call(answer_callback_query, callback_query["id"])
        _restore_keyboard(cur, callback_query, story_id)

    elif action == "mute1":
        cur.execute("UPDATE stories SET status = 'muted' WHERE id = ?", (story_id,))
        cur.execute("DELETE FROM follows WHERE story_id = ?", (story_id,))
        safe_call(answer_callback_query, callback_query["id"], text="Ignored.")
        _set_keyboard(callback_query, story_keyboard(story_id, muted=True))

    elif action == "mutetype":
        cur.execute("SELECT headline, summary FROM stories WHERE id = ?", (story_id,))
        story = cur.fetchone()
        if story is None:
            safe_call(answer_callback_query, callback_query["id"],
                      text="That story isn't available anymore.")
            return

        rule = describe_rule(story["headline"], story["summary"])
        if rule is None:
            # Fall back to muting just this story rather than doing nothing: the tap was
            # unambiguous even if naming the category wasn't.
            cur.execute("UPDATE stories SET status = 'muted' WHERE id = ?", (story_id,))
            safe_call(answer_callback_query, callback_query["id"],
                      text="Ignored this story (couldn't work out the category).")
            _set_keyboard(callback_query, story_keyboard(story_id, muted=True))
            return

        rule_id, created = add_rule(cur, rule, story_id,
                                    (story["summary"] or story["headline"] or "").strip()[:200])
        cur.execute("UPDATE stories SET status = 'muted' WHERE id = ?", (story_id,))
        cur.execute("DELETE FROM follows WHERE story_id = ?", (story_id,))

        safe_call(answer_callback_query, callback_query["id"], text="Ignored.")
        _set_keyboard(callback_query, story_keyboard(story_id, muted=True))

        # Say the rule out loud. A filter you can't see is one you can't correct, and
        # this one deletes things before you ever learn they existed.
        if not created:
            safe_call(send_message, chat_id,
                      f'Already ignoring stories about {rule}.', parse_mode=None)
            return

        # Then ask why -- but only after the rule is already in force, so an unanswered
        # question costs nothing. The category was guessed from a single headline, and
        # the headline cannot say whether "no more Powerball" means lotteries or means
        # gambling. The answer rewrites the rule and is kept alongside it, so every later
        # screening decision sees the user's own words rather than one inference from one
        # example. See mutes.refine_rule.
        cur.execute(
            "UPDATE preferences SET pending_why_rule_id = ?, pending_why_at = ? WHERE id = 1",
            (rule_id, now),
        )
        safe_call(
            send_message, chat_id,
            f"Got it — I'll stop showing stories about {rule}.\n\n"
            f"What is it you'd rather not see? A few words is plenty, and it'll make me "
            f"better at judging the next one. Or skip and I'll go with what I've got.\n\n"
            f'(Say "what am I ignoring" to review, or "unmute {rule_id}" to undo.)',
            reply_markup=why_keyboard(rule_id), parse_mode=None,
        )

    elif action == "unmute":
        cur.execute("UPDATE stories SET status = 'active' WHERE id = ?", (story_id,))
        # If this story was what created a category rule, undo lifts that too.
        lifted = deactivate_rule_for_story(cur, story_id)
        safe_call(answer_callback_query, callback_query["id"], text="Un-ignored.")
        _set_keyboard(callback_query, story_keyboard(story_id, following=False))
        if lifted:
            safe_call(send_message, chat_id,
                      f'Un-ignored — I\'ll show stories about {lifted} again.',
                      parse_mode=None)
