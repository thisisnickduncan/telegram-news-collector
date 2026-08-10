"""Claude summarization.

Two jobs:

  * summarize_pending_stories() -- the digest summary. Synthesizes across *all* the
    outlets that reported a story, not just the first headline we happened to cluster
    on, and says so explicitly when sources disagree on the facts.
  * expand_story() -- the deeper write-up produced when you reply to the end-of-digest
    prompt asking for more on a story. Uses web search, since "more coverage" means
    going past what GDELT already handed us.
"""
import sys
from pathlib import Path

import anthropic

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news_alert.config import ANTHROPIC_API_KEY
from news_alert.sources import independent_sources

MODEL = "claude-haiku-4-5"
EXPAND_MODEL = "claude-sonnet-5"

# Was 200, sized for SMS. This is Telegram, and a summary that has to reconcile several
# outlets (and flag where they disagree) needs the room -- but it still has to read as a
# glance on a phone, so it stays a short paragraph, not an article.
MAX_SUMMARY_CHARS = 400
MAX_EXPANDED_CHARS = 1200

# How many distinct source headlines to hand the model. Past a dozen it's repetition,
# and the cost is per-token on every story in every digest.
MAX_SOURCE_SNIPPETS = 12

SYSTEM_PROMPT = (
    "You write short, factual news summaries for a personal news digest. You are given "
    "the headlines several different outlets published about the SAME story.\n\n"
    "Write a single combined summary, 2-3 sentences, that reflects what the sources "
    "collectively report -- not a rewrite of any one headline. Lead with what actually "
    "happened.\n\n"
    "If the sources meaningfully disagree on a fact -- different numbers, different "
    "attribution of cause or blame, one reporting something the others contradict -- say "
    "so plainly in the summary (e.g. \"outlets differ on the death toll\" or \"only some "
    "sources attribute the strike to Israel\"). Do not paper over a real conflict, and do "
    "not manufacture one where the sources merely use different wording for the same fact.\n\n"
    "If only one headline is given, summarize from it alone -- never ask for more "
    "information and never refuse; always produce your best good-faith summary from what "
    "you're given.\n\n"
    "Plain text. State what happened, not your opinion of it -- no fluff, no "
    "editorializing, no hashtags, no emoji, no markdown. "
    f"Keep it under {MAX_SUMMARY_CHARS} characters."
)

EXPAND_SYSTEM = (
    "You are writing a deeper briefing on a news story for someone who already read a "
    "two-sentence summary of it and asked for more. Use web search to find current, "
    "accurate detail -- search silently, don't narrate your searches or think out loud "
    "between them.\n\n"
    "Respond with one final answer: 4-6 sentences of plain factual prose covering what "
    "happened, the essential context or background the short summary left out, and where "
    "things stand now. Note explicitly if reporting is contested or still developing. "
    "No headers, no bullet lists, no markdown, no meta-commentary about your research. "
    f"Keep it under {MAX_EXPANDED_CHARS} characters."
)


def _truncate(text, limit):
    """Trims to `limit`, preferring the last complete sentence.

    A hard character cut leaves a dangling fragment ("...over conflict. Trump appears…"),
    which reads like the message failed to send. Falling back to a whole sentence loses a
    clause but always reads finished. Only used when the model overshoots its brief.
    """
    if len(text) <= limit:
        return text

    head = text[:limit]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    # Only honour a sentence break in the back half, otherwise we'd throw away most of
    # the summary to avoid an ellipsis.
    if cut >= limit // 2:
        return head[:cut + 1]
    return head[:limit - 1].rsplit(" ", 1)[0] + "…"


# The prompt tells the model never to refuse, and it still occasionally does when a
# headline is truncated mid-sentence or in a script it can't parse -- one observed run
# put "I appreciate the request, but I'm unable to read the content from the URL..."
# into the digest as though it were the news. Cheaper and more reliable to detect that
# and fall back to the headline than to keep escalating the prompt.
_REFUSAL_MARKERS = (
    "i'm unable", "i am unable", "i cannot", "i can't", "i apologize", "i'm sorry",
    "could you provide", "could you please", "i would need", "i'd need",
    "unable to read", "unable to summarize", "no headline", "please provide",
    "the headline is incomplete", "as an ai",
    # Meta-commentary about the input rather than a summary of the news. These show up
    # when the grouper hands the summarizer a headline that doesn't belong with the
    # others -- the model helpfully narrates the mismatch instead of summarizing.
    "i can only", "i'll summarize", "i will summarize", "headlines about the same story",
    "completely different story", "unrelated story", "appears to be unrelated",
    "the first two headlines", "the third headline", "the fourth headline",
)


def _looks_like_refusal(text):
    lowered = text.lower()
    if any(marker in lowered for marker in _REFUSAL_MARKERS):
        return True
    # A summary is prose. Bullet lists and questions are the model talking to us about
    # the task rather than reporting the story.
    return text.lstrip().startswith(("- ", "* ", "1.")) or text.rstrip().endswith("?")


def _build_user_message(headline, sources):
    """sources: iterable of (title, outlet) pairs. Outlet is included so the model can
    attribute a disagreement to a specific publication rather than hedging vaguely."""
    seen, lines = set(), []
    for title, outlet in sources:
        if not title:
            continue
        key = title.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- {title}" + (f" ({outlet})" if outlet else ""))
        if len(lines) >= MAX_SOURCE_SNIPPETS:
            break

    if len(lines) <= 1:
        only = lines[0] if lines else f"- {headline}"
        return (f"Only one source reported this story:\n{only}\n\n"
                "Summarize from this alone.")

    return (f"{len(lines)} outlets reported this story. Their headlines:\n"
            + "\n".join(lines)
            + "\n\nWrite the combined summary.")


def summarize_story(headline, sources, client=None):
    client = client or anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=MODEL,
        max_tokens=400,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_user_message(headline, sources)}],
    )
    text = next((b.text for b in response.content if b.type == "text"), "").strip()
    if not text or _looks_like_refusal(text):
        print(f"[summarizer] discarding non-summary response for {headline!r}: {text[:120]!r}")
        return None
    return _truncate(text, MAX_SUMMARY_CHARS)


def summarize_pending_stories(cur, client=None, story_ids=None):
    """Summarizes stories with summary IS NULL -- i.e. not yet written up.

    story_ids narrows the work to the stories that will actually appear in this digest.
    A single run can create dozens of stories while the digest shows only a handful --
    one observed run summarized 42 to display 8. Pass None to summarize everything
    pending. Anything left unsummarized still renders: digest.py falls back to the
    headline.
    """
    client = client or anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    if story_ids is None:
        cur.execute("SELECT id, headline FROM stories WHERE summary IS NULL")
    elif not story_ids:
        return 0
    else:
        placeholders = ",".join("?" * len(story_ids))
        cur.execute(
            f"SELECT id, headline FROM stories WHERE summary IS NULL AND id IN ({placeholders})",
            tuple(story_ids),
        )
    pending = cur.fetchall()
    written = 0

    for story in pending:
        cur.execute(
            "SELECT title, outlet_name, domain, bias_rating FROM story_sources WHERE story_id = ?",
            (story["id"],),
        )
        # One headline per syndication group: showing the model the same wire copy nine
        # times invites it to describe a nonexistent consensus, and wastes the tokens.
        sources = [(r["title"], r["outlet_name"] or r["domain"])
                   for r in independent_sources(cur.fetchall())]
        summary = summarize_story(story["headline"], sources, client=client)
        if summary is None:
            # Left NULL on purpose: digest.py falls back to the headline, and a later
            # run gets another attempt once the story has more (or better) sources.
            continue
        cur.execute("UPDATE stories SET summary = ? WHERE id = ?", (summary, story["id"]))
        written += 1

    return written


def expand_story(cur, story_id, client=None):
    """Writes (and caches) the deeper briefing for a story you asked to go deeper on.

    Deliberately backed by web search rather than another GDELT pull: GDELT is the
    fragile, rate-saturated dependency in this pipeline and the run is already on a
    15-minute lead. Adding queries there would risk the delivery window; this doesn't.
    """
    cur.execute("SELECT headline, summary, expanded_summary FROM stories WHERE id = ?", (story_id,))
    story = cur.fetchone()
    if story is None:
        return None
    if story["expanded_summary"]:
        return story["expanded_summary"]

    cur.execute(
        """SELECT DISTINCT title, outlet_name, domain FROM story_sources
           WHERE story_id = ? AND title IS NOT NULL""",
        (story_id,),
    )
    source_lines = "\n".join(
        f"- {r['title']} ({r['outlet_name'] or r['domain']})" for r in cur.fetchall()
    )

    client = client or anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    user_content = (
        f"Story: {story['headline']}\n"
        f"Short summary already sent: {story['summary'] or '(none)'}\n"
        + (f"Known coverage:\n{source_lines}\n" if source_lines else "")
        + "\nWrite the deeper briefing."
    )
    response = client.messages.create(
        model=EXPAND_MODEL,
        max_tokens=1500,
        system=EXPAND_SYSTEM,
        tools=[{"type": "web_search_20260209", "name": "web_search"}],
        messages=[{"role": "user", "content": user_content}],
    )
    # With web_search on, the answer arrives split across several text blocks
    # interleaved with search calls -- concatenate all of them, not just the first.
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text or _looks_like_refusal(text):
        print(f"[summarizer] expand produced no usable text for story {story_id}: {text[:120]!r}")
        return None

    text = _truncate(text, MAX_EXPANDED_CHARS)
    cur.execute("UPDATE stories SET expanded_summary = ? WHERE id = ?", (text, story_id))
    return text


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from news_alert.db import db_cursor

    with db_cursor() as cur:
        n = summarize_pending_stories(cur)
        cur.execute("SELECT id, headline, summary FROM stories WHERE summary IS NOT NULL ORDER BY id")
        rows = cur.fetchall()

    print(f"Summarized {n} stories.\n")
    for r in rows:
        print(f"#{r['id']} {r['headline']}")
        print(f"   -> {r['summary']}\n")
