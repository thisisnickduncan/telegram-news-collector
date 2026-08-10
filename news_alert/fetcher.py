"""GDELT DOC 2.0 API pull against https://api.gdeltproject.org/api/v2/doc/doc.

Two kinds of search, run independently rather than crossed with each other:

  * one per tracked topic, with NO country filter -- you want the story wherever
    it breaks;
  * one per place in preferences.regions, searched on its own name, to pick up
    local news.

Crossing the two (the original design) meant 4 topics x 3 regions = 12 queries, most
of them meaningless -- '"Iran war" Hawaii' and the like. Since GDELT rate-limits
aggressively, each of those still burned a full retry cycle to return nothing; one
run took 26 minutes end to end. See fetch_all.

Country filtering, where it applies, uses GDELT's `sourcecountry:` operator, which
takes FIPS 10-4 two-letter codes (NOT ISO 3166 -- Germany is GM, not DE; Australia is
AS, not AU). Only a small, deliberately incomplete set of countries is mapped below.
A place that isn't a recognized country (e.g. "Hawaii", a US state) stays a plain
keyword search instead of being guessed at as a country code -- narrower results, but
not silently wrong. See plan section 11 for the same philosophy applied to bias ratings.
"""
import time

import requests

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

# GDELT asks for "one request every 5 seconds"; 6s keeps us clear of that for both
# spacing between searches and spacing between retries of the same search.
#
# The retry budget is deliberately "many attempts, short flat waits" rather than the
# usual few-attempts-with-escalating-backoff. Escalating backoff assumes a 429 means
# *you* are going too fast, so waiting longer helps. Measured against GDELT, that's
# not what's happening: 10 identical requests at 6s spacing from an otherwise idle
# host returned nine 429s and one 200, and a first request after minutes of silence
# still 429s. The API is simply saturated and answers probabilistically, so a longer
# wait is no likelier to succeed than a short one -- it just stalls the run. Extra
# attempts raise the odds a given topic lands at all; longer waits only cost time.
REQUEST_DELAY_SECONDS = 6.0
MAX_RETRIES = 6
RETRY_BACKOFF_SECONDS = 6.0
GLOBAL_REGION = "GLOBAL"

# GDELT's per-query ceiling is 250; we asked for 75. Raising it is free in the only
# currency that matters here -- it's the same number of HTTP requests, the same retry
# budget, the same saturation exposure, just a fuller response body.
#
# It matters because the digest now requires two independent outlets before it will
# show a story. Corroboration density is a function of how deeply each topic is
# sampled: at 75 records per topic, measured against the live database, only 8.5% of
# stories ever reached two distinct domains, because we were seeing the top slice of
# many events rather than several reports of the same one. Loosening the dedupe
# threshold does NOT fix that (measured: 80 -> 55 moved it 8% -> 17%, while inviting
# false merges); sampling deeper does.
MAX_RECORDS = 250

# GDELT indexes the whole world's press, so an unfiltered query returns whatever
# language the outlet published in. Two stories in the 2026-08-10 digests came back
# unreadable because of it: one was delivered to the phone as a raw Hebrew headline
# (the summarizer produced nothing, so digest.py fell back to the headline), and
# another as the summarizer narrating its own confusion -- "These appear to be
# translations of the same headline into different languages (German, Turkish...".
#
# Filtering at retrieval is the right layer: the summarizer can't write a good English
# summary from Turkish headlines, and the event grouper wastes Haiku calls clustering
# copy nobody will read. Verified against the live API -- `sourcelang:eng` is accepted
# and returned 250/250 English articles, i.e. no loss of volume, since the cap is what
# binds. (Codes are three-letter: `eng`, not `english`.)
SOURCE_LANGUAGE = "eng"

# FIPS 10-4 country codes for GDELT's sourcecountry: operator.
COUNTRY_FIPS = {
    "US": "US", "UNITED STATES": "US",
    "UK": "UK", "UNITED KINGDOM": "UK",
    "CANADA": "CA",
    "AUSTRALIA": "AS",
    "GERMANY": "GM",
    "FRANCE": "FR",
    "JAPAN": "JA",
    "CHINA": "CH",
    "INDIA": "IN",
    "RUSSIA": "RS",
    "BRAZIL": "BR",
    "MEXICO": "MX",
    "SOUTH AFRICA": "SF",
    "ITALY": "IT",
    "SPAIN": "SP",
    "SOUTH KOREA": "KS",
    "UKRAINE": "UP",
    "ISRAEL": "IS",
}


def _label(term, region):
    """How a single search is named in logs -- places and topics are distinct searches."""
    return f"place={term!r}" if region else f"topic={term!r}"


def _build_query(term, region=None, keywords=None, excluded_sources=None):
    """Builds one GDELT query for a single search term.

    `term` is either a tracked topic -- searched worldwide, with no country filter, so
    a story is caught wherever it breaks -- or a place from preferences.regions,
    searched on its own name. Topics and places are deliberately never crossed with
    each other; see fetch_all for why.
    """
    terms = [f'"{term}"' if " " in term else term]
    # Keywords narrow a topic search. They're left off place searches, which are
    # already narrow -- ANDing extra terms onto "Hawaii" mostly returns nothing.
    if keywords and not region:
        terms.extend(keywords)

    query = " ".join(terms)

    # Applies to topic and place searches alike -- an unreadable story is unreadable
    # whichever search found it. See SOURCE_LANGUAGE.
    if SOURCE_LANGUAGE:
        query += f" sourcelang:{SOURCE_LANGUAGE}"

    # A place that's a recognized country can additionally be pinned to that country's
    # outlets. Anything smaller (a state, a city) has no FIPS code and stays a plain
    # keyword search rather than being guessed at.
    if region:
        fips = COUNTRY_FIPS.get(region.strip().upper())
        if fips:
            query += f" sourcecountry:{fips}"

    for domain in excluded_sources or []:
        query += f" -domainis:{domain}"
    return query


def _get_with_retry(http, params, label):
    """GDELT 429s constantly regardless of how politely we ask -- just keep trying.

    The wait is flat, not escalating: see REQUEST_DELAY_SECONDS above for the
    measurements showing these 429s are saturation, not rate-limiting of us.
    """
    resp = http.get(GDELT_URL, params=params, timeout=30)
    attempt = 0
    while resp.status_code == 429 and attempt < MAX_RETRIES:
        attempt += 1
        print(f"[fetcher] 429 for {label}, retry {attempt}/{MAX_RETRIES} in "
              f"{RETRY_BACKOFF_SECONDS:.0f}s")
        time.sleep(RETRY_BACKOFF_SECONDS)
        resp = http.get(GDELT_URL, params=params, timeout=30)
    return resp


def fetch_articles(term, region=None, keywords=None, excluded_sources=None,
                    max_records=MAX_RECORDS, timespan="6h", session=None):
    """Hits GDELT for a single search term. Returns a list of dicts.

    `region` is set only for place searches; topic searches leave it None and their
    articles are labeled GLOBAL_REGION, since they're deliberately country-unfiltered.

    Every article carries a non-null `topic`, which dedupe.py uses to scope story
    matching -- for a place search that bucket is the place name itself, so local
    stories cluster with each other rather than with world news.
    """
    query = _build_query(term, region, keywords, excluded_sources)
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": max_records,
        "timespan": timespan,
    }
    http = session or requests
    resp = _get_with_retry(http, params, _label(term, region))
    resp.raise_for_status()

    try:
        data = resp.json()
    except ValueError:
        print(f"[fetcher] non-JSON response for {_label(term, region)} "
              f"query={query!r} -- skipping")
        return []

    results = []
    for article in data.get("articles", []):
        results.append({
            "title": article.get("title"),
            "url": article.get("url"),
            "domain": article.get("domain"),
            "seendate": article.get("seendate"),
            "sourcecountry": article.get("sourcecountry"),
            "topic": term,
            "region": region or GLOBAL_REGION,
        })
    return results


def fetch_all(preferences, max_records=MAX_RECORDS, timespan="6h"):
    """preferences: dict with topics/regions/keywords/excluded_sources (see news_alert.db.get_preferences).

    Issues one query per topic plus one per place -- NOT the cross-product of the two.
    Crossing them was the original design and it was both slow and useless: it turned
    4 topics x 3 regions into 12 queries, most of them nonsense like '"Iran war" Hawaii',
    and since GDELT rate-limits hard, each doomed query still burned a full retry cycle
    (~2 minutes) to return nothing. One 8pm run took 26 minutes to send.

    Topics are queried first, on purpose: they're the broad, high-value searches, and
    GDELT gets progressively stingier as a run goes on, so they should spend the
    rate-limit budget while it's still there.
    """
    topics = preferences["topics"] or []
    regions = preferences["regions"] or []
    keywords = preferences.get("keywords") or []
    excluded_sources = preferences.get("excluded_sources") or []

    searches = [(topic, None) for topic in topics] + [(region, region) for region in regions]

    all_articles = []
    for i, (term, region) in enumerate(searches):
        if i:
            time.sleep(REQUEST_DELAY_SECONDS)

        try:
            articles = fetch_articles(
                term, region=region,
                keywords=keywords,
                excluded_sources=excluded_sources,
                max_records=max_records,
                timespan=timespan,
            )
        except requests.RequestException as exc:
            print(f"[fetcher] request failed for {_label(term, region)}: {exc}")
            continue

        print(f"[fetcher] {_label(term, region)} -> {len(articles)} articles")
        all_articles.extend(articles)

    return all_articles


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from news_alert.db import get_preferences

    prefs = get_preferences()
    if prefs is None:
        print("No preferences set. Run scripts/set_preferences.py first.")
        sys.exit(1)

    articles = fetch_all(prefs)
    print(f"\nTotal articles fetched: {len(articles)}")
    for a in articles:
        print(f"  [{a['region']}/{a['topic']}] {a['title']}  ({a['domain']})")
