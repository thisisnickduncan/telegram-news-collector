"""Handles incoming free-text Telegram messages (not button presses).

A message can mean three different things, and there's no command syntax to tell them
apart, so intent is classified rather than assumed:

- If you tapped "Ask" on a story, your next message answers that question --
  Claude with live web search, since a one-off question deserves a real answer,
  not just a rephrase of what we already fetched. This takes precedence over
  everything else: you pressed a button, the intent isn't ambiguous.
- Otherwise the message is either a reply to the digest's closing "which of these do
  you want more on?" prompt, or a request to start tracking a new topic. One Claude
  call decides which, with the last digest's stories in front of it -- "the Iran one"
  only resolves if you can see what was in the digest.

Digest replies are queued (see deep_dive.py) rather than acted on immediately, so an
answer that arrives three hours later still steers the next run.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news_alert.config import ANTHROPIC_API_KEY
from news_alert.deep_dive import last_digest_stories, queue_stories
from news_alert.telegram_client import safe_call, send_message

TOPIC_MODEL = "claude-haiku-4-5"
INTENT_MODEL = "claude-haiku-4-5"
QA_MODEL = "claude-sonnet-5"

TOPIC_EXTRACTION_SYSTEM = (
    "The user is messaging a personal news-alert bot that tracks topics and sends "
    "periodic digests. Given their message, decide if they're asking to start tracking "
    "a news topic (e.g. \"what's going on with the Iran war\", \"keep an eye on Fed rate "
    "decisions\"). If so, respond with ONLY a short topic phrase suitable as a search "
    "query (e.g. \"Iran war\", \"Fed rate decisions\"), nothing else -- no punctuation, "
    "no quotes, no explanation. If the message is NOT a topic request (small talk, "
    "unclear, a question about something unrelated), respond with exactly: NONE"
)

INTENT_SYSTEM = (
    "You route replies to a personal news-alert bot. The bot sends a digest of stories "
    "every 4 hours and ends each one by asking which stories the user wants deeper "
    "coverage on next time. The user may answer that question, or may instead ask the bot "
    "to start tracking a brand-new topic.\n\n"
    "You are given the stories from the last digest (numbered) and the user's message. "
    "Reply with EXACTLY ONE line, in one of these three forms:\n\n"
    "DEEPDIVE <numbers>   -- the message is picking stories from the list above. Use the "
    "numbers shown, comma-separated, e.g. \"DEEPDIVE 1,4\". If they say something like "
    "\"all of them\" or \"everything\", list every number. Match on meaning, not exact "
    "wording -- \"the Iran one\", \"that cyber story\", \"the second one\" all count.\n"
    "TOPIC <phrase>       -- the message asks to follow a NEW subject that isn't one of "
    "the listed stories. Give a short search-ready phrase, e.g. \"TOPIC Fed rate decisions\".\n"
    "NONE                 -- small talk, unclear, or unrelated to either.\n\n"
    "Prefer DEEPDIVE when the message plainly refers to stories in the list. Prefer TOPIC "
    "when it names a subject that isn't there. Output nothing but that single line."
)

QA_SYSTEM = (
    "You answer follow-up questions about a specific news story for someone who just "
    "read a short summary of it, over SMS-style chat. Use web search to find current, "
    "accurate information -- search silently, don't narrate what you're searching for "
    "or think out loud between searches. Respond with exactly one final answer: a few "
    "sentences of plain factual text, no headers or bullet lists unless the question "
    "genuinely calls for a list, no markdown formatting, no meta-commentary about your "
    "research process."
)


def _extract_topic(text, client):
    response = client.messages.create(
        model=TOPIC_MODEL,
        max_tokens=50,
        system=TOPIC_EXTRACTION_SYSTEM,
        messages=[{"role": "user", "content": text}],
    )
    reply = next((b.text for b in response.content if b.type == "text"), "").strip()
    print(f"[message_handler] topic extraction on {text!r} -> {reply!r}")
    return None if reply.upper() == "NONE" or not reply else reply


def _classify_reply(text, stories, client):
    """Decides between "picking stories from the last digest" and "track a new topic".

    Returns ("deep_dive", [story_id, ...]) | ("topic", phrase) | ("none", None).

    The model is shown 1-based positions rather than raw story ids and its answer is
    mapped back here -- a hallucinated "story 4" in a 3-story digest is then simply
    dropped, where a hallucinated primary key would silently queue the wrong story.
    """
    if not stories:
        return "topic", None      # nothing to pick from; fall through to topic handling

    listing = "\n".join(
        f"{i}. {(s['summary'] or s['headline'] or '').strip()}"
        + (f"  [topic: {s['topic']}]" if s.get("topic") else "")
        for i, s in enumerate(stories, start=1)
    )
    response = client.messages.create(
        model=INTENT_MODEL,
        max_tokens=100,
        system=INTENT_SYSTEM,
        messages=[{"role": "user",
                   "content": f"Last digest:\n{listing}\n\nUser's message: {text}"}],
    )
    reply = next((b.text for b in response.content if b.type == "text"), "").strip()
    print(f"[message_handler] intent classification on {text!r} -> {reply!r}")

    upper = reply.upper()
    if upper.startswith("DEEPDIVE"):
        picked = []
        for token in reply[len("DEEPDIVE"):].replace(",", " ").split():
            if token.isdigit():
                index = int(token)
                if 1 <= index <= len(stories):
                    story_id = stories[index - 1]["id"]
                    if story_id not in picked:
                        picked.append(story_id)
        return ("deep_dive", picked) if picked else ("none", None)

    if upper.startswith("TOPIC"):
        phrase = reply[len("TOPIC"):].strip(" :-") or None
        return "topic", phrase

    return "none", None


def _handle_deep_dive_request(cur, chat_id, story_ids, stories, client):
    queued = queue_stories(cur, story_ids)
    by_id = {s["id"]: s for s in stories}

    if not queued:
        safe_call(send_message, chat_id,
                  "Already queued those -- they'll lead your next digest.", parse_mode=None)
        return

    labels = []
    for story_id in queued:
        story = by_id.get(story_id)
        text = ((story or {}).get("summary") or (story or {}).get("headline") or "").strip()
        labels.append(text[:70] + ("…" if len(text) > 70 else ""))

    listing = "\n".join(f"• {label}" for label in labels)
    plural = "these" if len(queued) > 1 else "this"
    safe_call(
        send_message, chat_id,
        f"Got it -- I'll pull deeper coverage on {plural} and lead your next digest with "
        f"{'them' if len(queued) > 1 else 'it'}:\n{listing}",
        parse_mode=None,
    )


def _handle_topic_request(cur, chat_id, text, client, topic=None):
    """topic: the phrase the intent classifier already pulled out, when it ran. Saves a
    second Claude call on the common path; None means fall back to extracting here."""
    print(f"[message_handler] handling as topic request: {text!r} (pre-extracted={topic!r})")
    if topic is None:
        topic = _extract_topic(text, client)
    if topic is None:
        safe_call(
            send_message, chat_id,
            "I didn't catch a topic there. Try something like \"the Iran war\", "
            "or tap Ask under a story to ask about it specifically.",
            parse_mode=None,
        )
        return

    cur.execute("SELECT topics FROM preferences WHERE id = 1")
    topics = json.loads(cur.fetchone()["topics"])
    if topic.lower() in (t.lower() for t in topics):
        safe_call(send_message, chat_id, f'Already tracking "{topic}" -- you\'ll keep seeing it in your updates.',
                   parse_mode=None)
        return

    topics.append(topic)
    cur.execute(
        "UPDATE preferences SET topics = ?, updated_at = ? WHERE id = 1",
        (json.dumps(topics), datetime.now(timezone.utc).isoformat()),
    )
    safe_call(
        send_message, chat_id,
        f'Got it -- I\'ll start tracking "{topic}" coverage. You\'ll see it in your next '
        f"update and every one after, until you tell me to stop.",
        parse_mode=None,
    )


def _answer_story_question(cur, chat_id, story_id, question, client):
    print(f"[message_handler] answering question about story_id={story_id}: {question!r}")
    cur.execute("SELECT headline, summary FROM stories WHERE id = ?", (story_id,))
    story = cur.fetchone()
    cur.execute("UPDATE preferences SET pending_ask_story_id = NULL WHERE id = 1")

    if story is None:
        print(f"[message_handler] story_id={story_id} not found -- can't answer")
        safe_call(send_message, chat_id, "That story isn't available anymore. What else can I help with?",
                  parse_mode=None)
        return

    cur.execute(
        "SELECT DISTINCT title, domain FROM story_sources WHERE story_id = ? AND title IS NOT NULL",
        (story_id,),
    )
    source_lines = "\n".join(f"- {row['title']} ({row['domain']})" for row in cur.fetchall())

    user_content = (
        f"Story: {story['headline']}\n"
        f"Summary: {story['summary'] or '(no summary yet)'}\n"
        + (f"Sources:\n{source_lines}\n" if source_lines else "")
        + f"\nQuestion: {question}"
    )

    print(f"[message_handler] QA prompt:\n{user_content}")
    response = client.messages.create(
        model=QA_MODEL,
        max_tokens=1024,
        system=QA_SYSTEM,
        tools=[{"type": "web_search_20260209", "name": "web_search"}],
        messages=[{"role": "user", "content": user_content}],
    )
    block_types = [b.type for b in response.content]
    print(f"[message_handler] QA response stop_reason={response.stop_reason} block_types={block_types}")
    # With web_search enabled, the answer can be split across several text blocks
    # interleaved with search calls -- concatenate all of them, not just the first.
    answer = "".join(b.text for b in response.content if b.type == "text").strip()
    if not answer:
        print(f"[message_handler] QA produced no text block -- full content: {response.content}")
        answer = "I wasn't able to find a good answer to that -- try rephrasing?"
    print(f"[message_handler] QA answer: {answer!r}")

    safe_call(send_message, chat_id, answer, parse_mode=None)


def handle_message(cur, message, client=None):
    client = client or anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()
    if not text:
        return

    cur.execute("SELECT pending_ask_story_id FROM preferences WHERE id = 1")
    row = cur.fetchone()
    if row is None:
        print(f"[message_handler] no preferences row -- ignoring message from chat_id={chat_id}")
        return

    print(f"[message_handler] received {text!r} from chat_id={chat_id}, "
          f"pending_ask_story_id={row['pending_ask_story_id']}")

    # An explicit Ask button press wins outright -- you told us what you meant.
    if row["pending_ask_story_id"] is not None:
        _answer_story_question(cur, chat_id, row["pending_ask_story_id"], text, client)
        return

    # Otherwise this is either an answer to the digest's closing prompt or a new topic.
    # The last digest stays the active target until the next one goes out, which is what
    # makes a reply three hours later still land on the right stories.
    stories = last_digest_stories(cur)
    intent, payload = _classify_reply(text, stories, client)
    print(f"[message_handler] intent={intent!r} payload={payload!r}")

    if intent == "deep_dive":
        _handle_deep_dive_request(cur, chat_id, payload, stories, client)
    elif intent == "topic":
        _handle_topic_request(cur, chat_id, text, client, topic=payload)
    else:
        safe_call(
            send_message, chat_id,
            "Not sure what you meant there. You can name a story from the last digest to "
            "get more on it, ask me to track a new topic, or tap Ask under any story.",
            parse_mode=None,
        )
