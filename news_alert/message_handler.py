"""Handles incoming free-text Telegram messages (not button presses):

- If you tapped "Ask" on a story, your next message answers that question --
  Claude with live web search, since a one-off question deserves a real answer,
  not just a rephrase of what we already fetched.
- Otherwise, treat the message as a request to start tracking a new topic --
  Claude extracts a search-ready topic phrase (or decides it's not a topic
  request at all) and, if it is one, the topic is added to preferences.topics
  permanently, so it shows up in every future digest until stopped.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news_alert.config import ANTHROPIC_API_KEY
from news_alert.telegram_client import safe_call, send_message

TOPIC_MODEL = "claude-haiku-4-5"
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


def _handle_topic_request(cur, chat_id, text, client):
    print(f"[message_handler] handling as topic request: {text!r}")
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

    if row["pending_ask_story_id"] is not None:
        _answer_story_question(cur, chat_id, row["pending_ask_story_id"], text, client)
    else:
        _handle_topic_request(cur, chat_id, text, client)
