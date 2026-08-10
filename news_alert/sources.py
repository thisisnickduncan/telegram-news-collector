"""Collapsing syndicated copies of one article into a single source.

A story's source list is not a list of independent reports. Measured on real fetched
data, the most "corroborated" story in the database had nine distinct domains and
exactly ONE distinct headline: wxii12, wlwt, wisn, wyff4 and five more are all Hearst
Television affiliates running the same wire piece at the same URL path. The UK
equivalents (Reach plc: bristolpost / chroniclelive / gazettelive / somersetlive) and
the Fox O&O group behave the same way.

Counting those as nine sources would make the digest's central promise false: a story
carried by one syndicate would look like a consensus of nine outlets, and the coverage
breakdown would report "9 Center" for what is a single article. Syndicated copies also
cannot disagree with each other, so the summarizer has nothing to synthesize.

So sources are grouped by near-identical headline and each group votes once. Two
outlets that wrote their own headline about the same event are two voices; nine
outlets running the same headline are one.
"""
import re

from rapidfuzz import fuzz

from news_alert.dedupe import normalize_title

# Trailing masthead: "Art the Clown takes over Universal Terror Tram – Press Telegram".
# Publisher groups syndicate one article across their whole chain and each paper appends
# its own name, which is enough to drag five identical copies from ~100 down to 75 --
# under the syndication threshold, so they counted as five independent outlets and
# manufactured a coverage spread out of one article. Stripping the masthead first puts
# them back at ~100 where they belong.
#
# Only en/em dash, pipe and underscore are treated as masthead separators. A plain
# hyphen is excluded on purpose: GDELT pads punctuation with spaces, so real headline
# text routinely contains " - " ("coming - of - age").
_MASTHEAD_RE = re.compile(r"\s[–—|_]\s+[^–—|_]{1,40}$")


def grouping_key(title):
    """Normalized headline with any trailing masthead removed, for syndication compare."""
    text = title or ""
    # Loop: some feeds carry two ("... _ 新闻频道 _ 中华网").
    for _ in range(3):
        stripped = _MASTHEAD_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    return normalize_title(text)

# Headlines at or above this similarity are treated as the same piece of copy.
# Deliberately high: the goal is catching republished copy, not merging two outlets'
# genuinely different takes. (dedupe.MATCH_THRESHOLD, at 80, is the looser test for
# "same event" -- this is the stricter test for "same article".)
SYNDICATION_THRESHOLD = 90


def _rank(row):
    """Preference order for which copy represents its syndication group: a rated outlet
    first (it can vote on the coverage breakdown), then the shortest domain, which tends
    to be the parent rather than a local affiliate."""
    rated = 0 if (row["bias_rating"] and row["bias_rating"] != "unrated") else 1
    return (rated, len(row["domain"] or ""), row["domain"] or "")


def group_sources(rows):
    """Groups source rows by near-identical headline.

    Returns a list of groups, each {"rep": row, "rows": [...], "domains": {..}},
    ordered by group size descending -- the most widely syndicated first.
    """
    groups = []
    for row in rows:
        if not row["title"]:
            continue
        normalized = grouping_key(row["title"])
        for group in groups:
            if fuzz.token_sort_ratio(normalized, group["key"]) >= SYNDICATION_THRESHOLD:
                group["rows"].append(row)
                group["domains"].add((row["domain"] or "").lower())
                break
        else:
            groups.append({
                "key": normalized,
                "rows": [row],
                "domains": {(row["domain"] or "").lower()},
            })

    for group in groups:
        group["rep"] = min(group["rows"], key=_rank)
    groups.sort(key=lambda g: len(g["domains"]), reverse=True)
    return groups


def independent_sources(rows):
    """One representative row per syndication group, and at most one per outlet.

    Both passes are needed and they catch different things. Grouping by headline removes
    one article republished across many domains; the per-domain pass removes the mirror
    case -- one outlet running several *different* pieces on a story. Two New York Post
    opinion columns about Newsom are two headlines and one voice, and counting them twice
    would have let a single outlet carry a story past the two-source bar on its own, and
    weighted its lean double in the coverage breakdown.
    """
    seen, reps = set(), []
    for group in group_sources(rows):
        domain = (group["rep"]["domain"] or "").lower()
        if domain and domain in seen:
            continue
        seen.add(domain)
        reps.append(group["rep"])
    return reps


def count_independent(rows):
    """How many independent outlets back this story. This is the number the digest's
    minimum-sources rule is applied to, not the raw domain count."""
    return len(independent_sources(rows))
