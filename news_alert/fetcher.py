"""GDELT DOC 2.0 API pull, region-aware.

One query per (topic, region) pair against https://api.gdeltproject.org/api/v2/doc/doc.
Region filtering uses GDELT's `sourcecountry:` operator, which takes FIPS 10-4
two-letter codes (NOT ISO 3166 -- e.g. Germany is GM, not DE; Australia is AS, not AU).
Only a small, deliberately incomplete set of countries is mapped below. A region that
isn't a recognized country (e.g. "Hawaii", a US state) is folded into the query as a
plain keyword term instead of guessed at as a country code -- narrower results, but
not silently wrong. See plan section 11 for the same philosophy applied to bias ratings.
"""
import time

import requests

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
REQUEST_DELAY_SECONDS = 15.0
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 20.0
GLOBAL_REGION = "GLOBAL"

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


def _build_query(topic, region, keywords=None, excluded_sources=None):
    terms = [f'"{topic}"' if " " in topic else topic]
    if keywords:
        terms.extend(keywords)

    fips = None
    region_upper = (region or "").strip().upper()
    if region_upper and region_upper != GLOBAL_REGION:
        fips = COUNTRY_FIPS.get(region_upper)
        if fips is None:
            terms.append(region)

    query = " ".join(terms)
    if fips:
        query += f" sourcecountry:{fips}"
    for domain in excluded_sources or []:
        query += f" -domainis:{domain}"
    return query


def _get_with_retry(http, params, topic, region):
    """GDELT 429s in practice even at conservative request rates -- back off and retry."""
    resp = http.get(GDELT_URL, params=params, timeout=30)
    attempt = 0
    while resp.status_code == 429 and attempt < MAX_RETRIES:
        attempt += 1
        wait = RETRY_BACKOFF_SECONDS * attempt
        print(f"[fetcher] 429 for topic={topic!r} region={region!r}, "
              f"retry {attempt}/{MAX_RETRIES} in {wait:.0f}s")
        time.sleep(wait)
        resp = http.get(GDELT_URL, params=params, timeout=30)
    return resp


def fetch_articles(topic, region, keywords=None, excluded_sources=None,
                    max_records=75, timespan="6h", session=None):
    """Hits GDELT for a single (topic, region) pair. Returns a list of dicts."""
    query = _build_query(topic, region, keywords, excluded_sources)
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": max_records,
        "timespan": timespan,
    }
    http = session or requests
    resp = _get_with_retry(http, params, topic, region)
    resp.raise_for_status()

    try:
        data = resp.json()
    except ValueError:
        print(f"[fetcher] non-JSON response for topic={topic!r} region={region!r} "
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
            "topic": topic,
            "region": region,
        })
    return results


def fetch_all(preferences, max_records=75, timespan="6h"):
    """preferences: dict with topics/regions/keywords/excluded_sources (see news_alert.db.get_preferences)."""
    topics = preferences["topics"]
    regions = preferences["regions"] or [GLOBAL_REGION]
    keywords = preferences.get("keywords") or []
    excluded_sources = preferences.get("excluded_sources") or []

    all_articles = []
    first_request = True
    for topic in topics:
        for region in regions:
            if not first_request:
                time.sleep(REQUEST_DELAY_SECONDS)
            first_request = False

            try:
                articles = fetch_articles(
                    topic, region,
                    keywords=keywords,
                    excluded_sources=excluded_sources,
                    max_records=max_records,
                    timespan=timespan,
                )
            except requests.RequestException as exc:
                print(f"[fetcher] request failed for topic={topic!r} region={region!r}: {exc}")
                continue

            print(f"[fetcher] topic={topic!r} region={region!r} -> {len(articles)} articles")
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
