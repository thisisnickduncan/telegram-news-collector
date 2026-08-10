"""How much a clustered story is actually about the term that found it.

GDELT matches a query against an article's full text, not its subject, so a search for
"California" returns anything that mentions California once in passing. Measured on the
live database: of 900 headlines fetched under 'California', only 9% mention California
at all; 'Hawaii' 4%, 'cybersecurity' 13%, 'Iran war' 23%. That is why a digest searching
for California news delivered a story about Chinese AI companion apps.

This produces a RANKING signal, deliberately not a filter. Requiring the term to appear
in a headline would have dropped 11 of the 16 stories delivered on 2026-08-10, and it
drops true positives as well -- "Four U.S. states are suing Meta" is the California case,
and never says so. Since the two-source rule already holds supply to roughly eight
stories a run, gating on topicality would starve the digest and start firing the
single-source floor, which is a worse outcome than an off-topic story sitting at the
bottom. So on-topic stories sort to the top and drift fills whatever is left.

No alias tables. It is tempting to teach it that Los Angeles implies California, but the
regions are whatever the user typed into preferences, so a hand-written gazetteer would
cover today's list and silently do nothing for tomorrow's. Scoring on the term's own
words is weaker per-story and honest everywhere.
"""
import re

# Words too generic to carry topicality on their own. A story matching only "war" out of
# "Iran war" is as likely to be about Ukraine.
STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "in", "on", "for", "to", "at", "by", "with",
    "from", "new", "news", "latest", "update", "updates",
}

MIN_WORD_LENGTH = 3

_WORD_RE = re.compile(r"[a-z0-9]+")


def significant_words(term):
    """The words in a search term that actually carry its subject."""
    words = _WORD_RE.findall((term or "").lower())
    return [w for w in words if len(w) >= MIN_WORD_LENGTH and w not in STOPWORDS]


def _matches(word, text):
    """Whether `word` appears in `text` as a word, allowing an inflected ending.

    Prefix rather than exact match so "California" catches "Californian" and
    "Californians", and "regulation" catches "regulations". Bounded on the left only:
    matching inside a word would let "iran" hit "tyrannical".
    """
    return re.search(rf"\b{re.escape(word)}", text) is not None


def topic_score(term, titles):
    """How many of the term's significant words show up across a story's headlines.

    Graded rather than boolean on purpose. Picking a single keyword out of a multi-word
    term has no safe rule -- the longest word of "Iran war" is "iran", which is right,
    but the longest word of "hawaii politics" is "politics", which is useless. Counting
    how many of them land sidesteps the choice: a story naming both ranks above one
    naming either, and both rank above one naming neither.
    """
    words = significant_words(term)
    if not words:
        return 0
    haystack = " ".join((t or "").lower() for t in titles)
    return sum(1 for word in words if _matches(word, haystack))
