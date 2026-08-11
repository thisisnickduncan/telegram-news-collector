"""Domain -> political-lean lookup, and the per-story coverage breakdown.

Two datasets, deliberately layered:

  * AllSides (favstats/AllSideR CSV) -- ~100 hand-mapped outlets. Rates outlets *by
    name*, not domain, so DOMAIN_TO_ALLSIDES_NAME is a curated bridge. High quality,
    narrow.
  * Media Bias/Fact Check (drmikecrowe/mbfcext combined.json) -- ~8.8k domains, already
    keyed by domain. Much broader, coarser. Measured against real fetched articles, it
    lifts rated coverage from ~7% of source rows to ~46%.

AllSides wins when both rate a domain (source_bias.rating_source records which one
supplied the value). Outlets in NEITHER dataset are left unrated and are simply
excluded from the coverage math -- never guessed at, never shown as "Unrated". They
still appear in a story's source links; they just don't vote on its lean.

The remaining unrated share is mostly non-US and trade press (chinatimes.com,
jang.com.pk, thepoultrysite.com...). That's an honest limitation, not a bug.
"""
import csv
import io
import json
from datetime import datetime, timezone

import requests

ALLSIDES_CSV_URL = "https://raw.githubusercontent.com/favstats/AllSideR/master/data/allsides_data.csv"
MBFC_JSON_URL = "https://raw.githubusercontent.com/drmikecrowe/mbfcext/main/docs/v4/combined.json"

# Canonical lean categories and their position on the AllSides -6..+6 scale.
#
# NB: the AllSides CSV's own `rating_num` column is a 1-5 ordinal (left=1 ... right=5),
# NOT the -6..+6 scale AllSides publishes. Averaging that raw column would put "center"
# at 3.0 and make every story look right-leaning. We map the *category* onto -6..+6
# ourselves so both datasets land on one comparable scale.
LEAN_NUMERIC = {
    "left": -6.0,
    "left-center": -3.0,
    "center": 0.0,
    "right-center": 3.0,
    "right": 6.0,
}

# Display bucket for the count line. Leans collapse into their side, matching how
# Ground News presents it ("3 Left · 1 Center · 2 Right").
LEAN_BUCKET = {
    "left": "Left",
    "left-center": "Left",
    "center": "Center",
    "right-center": "Right",
    "right": "Right",
}
BUCKET_ORDER = ("Left", "Center", "Right")
BUCKET_BLOCK = {"Left": "\U0001F7E6", "Center": "\U00002B1C", "Right": "\U0001F7E5"}
# Five blocks, not ten. Ten emoji squares wrapped on a narrow phone and read as a chart
# in their own right; the bar is a glance-level hint next to a one-word lean label, and at
# five it sits on one line beside it.
BAR_WIDTH = 5

# Below this many rated voices there's no spread worth drawing -- see story_coverage.
MIN_RATED_FOR_COVERAGE = 2

# MBFC's compact bias codes -> canonical category. Only the five political-lean codes
# are mapped. PS (pro-science), FN (fake news), CP (conspiracy) and S (satire) are
# credibility judgements, not left-right positions, so they contribute no lean.
MBFC_CODE_TO_RATING = {
    "L": "left",
    "LC": "left-center",
    "C": "center",
    "RC": "right-center",
    "R": "right",
}

# Domains where stripping a subdomain would be wrong: the parent is a hosting platform,
# not a publisher, so someblog.substack.com must never inherit substack.com's rating.
PLATFORM_DOMAINS = {
    "substack.com", "wordpress.com", "blogspot.com", "medium.com", "tumblr.com",
    "wixsite.com", "squarespace.com", "ghost.io", "github.io", "typepad.com",
    "livejournal.com", "weebly.com", "blogspot.co.uk", "newsvine.com",
}

# Hand-added aliases, merged with the alias map MBFC ships. Left-hand side is what
# GDELT hands us; right-hand side is the domain the datasets key on.
DOMAIN_ALIASES = {
    "edition.cnn.com": "cnn.com",
    "us.cnn.com": "cnn.com",
    "amp.cnn.com": "cnn.com",
    "money.cnn.com": "cnn.com",
    "www.bbc.co.uk": "bbc.com",
    "bbc.co.uk": "bbc.com",
    "news.sky.com": "sky.com",
    "abcnews.go.com": "abcnews.go.com",
    "apnews.com": "apnews.com",
    "nyti.ms": "nytimes.com",
    "wapo.st": "washingtonpost.com",
    "politi.co": "politico.com",
    "reut.rs": "reuters.com",
    "on.wsj.com": "wsj.com",
    "cnb.cx": "cnbc.com",
    "nbcnews.to": "nbcnews.com",
    "fxn.ws": "foxnews.com",
    "huffp.st": "huffpost.com",
    "theguardian.co.uk": "theguardian.com",
    "amp.theguardian.com": "theguardian.com",
}

DOMAIN_TO_ALLSIDES_NAME = {
    "nytimes.com": "New York Times - News",
    "foxnews.com": "Fox Online News",
    "cnn.com": "CNN (Web News)",
    "reuters.com": "Reuters",
    "apnews.com": "Associated Press",
    "bbc.com": "BBC News",
    "bbc.co.uk": "BBC News",
    "wsj.com": "Wall Street Journal - News",
    "washingtonpost.com": "Washington Post",
    "npr.org": "NPR Online News",
    "breitbart.com": "Breitbart News",
    "huffpost.com": "HuffPost",
    "politico.com": "Politico",
    "theguardian.com": "The Guardian",
    "usatoday.com": "USA TODAY",
    "nbcnews.com": "NBCNews.com",
    "cbsnews.com": "CBS News",
    "abcnews.go.com": "ABC News",
    "msnbc.com": "MSNBC",
    "forbes.com": "Forbes",
    "bloomberg.com": "Bloomberg",
    "axios.com": "Axios",
    "vox.com": "Vox",
    "thehill.com": "The Hill",
    "nypost.com": "New York Post",
    "dailywire.com": "The Daily Wire",
    "motherjones.com": "Mother Jones",
    "thefederalist.com": "The Federalist",
    "newsmax.com": "Newsmax",
    "oann.com": "One America News Network",
    "propublica.org": "ProPublica",
    "slate.com": "Slate",
    "theatlantic.com": "The Atlantic",
    "economist.com": "The Economist",
    "ft.com": "Financial Times",
    "aljazeera.com": "Al Jazeera",
    "businessinsider.com": "Business Insider",
    "time.com": "Time Magazine",
    "dailycaller.com": "The Daily Caller",
    "dailykos.com": "Daily Kos",
    "theintercept.com": "The Intercept",
    "nationalreview.com": "National Review",
    "reason.com": "Reason",
    "newyorker.com": "The New Yorker",
    "independent.co.uk": "The Independent",
    "telegraph.co.uk": "The Telegraph - UK",
    "newsweek.com": "Newsweek",
    "dailymail.co.uk": "Daily Mail",
    "latimes.com": "Los Angeles Times",
    "chicagotribune.com": "Chicago Tribune",
    "bostonglobe.com": "The Boston Globe",
    "freebeacon.com": "Washington Free Beacon",
    "washingtonexaminer.com": "Washington Examiner",
    "washingtontimes.com": "Washington Times",
    "theepochtimes.com": "The Epoch Times",
    "salon.com": "Salon",
    "thenation.com": "The Nation",
    "rollcall.com": "Roll Call",
    "realclearpolitics.com": "RealClearPolitics",
    "fivethirtyeight.com": "FiveThirtyEight",
    "marketwatch.com": "MarketWatch",
    "cnbc.com": "CNBC",
    "techcrunch.com": "TechCrunch",
    "theverge.com": "The Verge",
    "vanityfair.com": "Vanity Fair",
    "rollingstone.com": "RollingStone.com",
    "thegatewaypundit.com": "The Gateway Pundit",
    "infowars.com": "InfoWars",
    "thepostmillennial.com": "The Post Millennial",
    "redstate.com": "Red State",
    "townhall.com": "Townhall",
    "theblaze.com": "TheBlaze.com",
    "dailysignal.com": "The Daily Signal",
    "pbs.org": "PBS NewsHour",
    "democracynow.org": "Democracy Now",
    "jpost.com": "The Jerusalem Post",
    "koreaherald.com": "The Korea Herald",
    "scientificamerican.com": "Scientific American",
    "nationalinterest.org": "National Interest",
    "foreignaffairs.com": "Foreign Affairs",
    "grist.org": "Grist",
    "qz.com": "Quartz",
    "mashable.com": "Mashable",
    "lifehacker.com": "Lifehacker",
    "teenvogue.com": "Teen Vogue",
    "jacobinmag.com": "Jacobin",
    "commentary.org": "Commentary Magazine",
    "cjr.org": "Columbia Journalism Review",
    "politifact.com": "PolitiFact",
    "factcheck.org": "FactCheck.org",
    "opensecrets.org": "OpenSecrets.org",
    "newrepublic.com": "New Republic",
    "thedailybeast.com": "Daily Beast",
    "truthout.org": "TruthOut",
    "truthdig.com": "Truthdig",
    "theroot.com": "The Root",
    "jezebel.com": "Jezebel",
    "bustle.com": "Bustle",
    "upworthy.com": "Upworthy",
}


def domain_candidates(domain):
    """Lookup keys for one raw domain, most specific first.

    GDELT reports whatever host the article sat on, so the same outlet arrives as
    cnn.com, edition.cnn.com and amp.cnn.com. We try the exact host, then its alias,
    then progressively shorter parent domains -- stopping before hosting platforms,
    where the parent is not the publisher.
    """
    if not domain:
        return []
    host = domain.strip().lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return []

    seen, candidates = set(), []

    def add(value):
        if value and value not in seen and value not in PLATFORM_DOMAINS:
            seen.add(value)
            candidates.append(value)

    add(host)
    alias = DOMAIN_ALIASES.get(host)
    if alias:
        add(alias)

    parts = host.split(".")
    # Stop at 2 labels: one more strip would leave a bare TLD.
    for i in range(1, max(len(parts) - 1, 1)):
        parent = ".".join(parts[i:])
        if parent in PLATFORM_DOMAINS:
            break
        add(parent)
        alias = DOMAIN_ALIASES.get(parent)
        if alias:
            add(alias)

    return candidates


def get_bias_for_domain(cur, domain):
    """Returns {domain, outlet_name, rating, rating_num, rating_source} or None.

    Tries each normalized candidate in specificity order, so edition.cnn.com resolves
    via cnn.com but someblog.substack.com resolves to nothing.
    """
    for candidate in domain_candidates(domain):
        cur.execute("SELECT * FROM source_bias WHERE domain = ?", (candidate,))
        row = cur.fetchone()
        if row is not None:
            return {
                "domain": row["domain"],
                "outlet_name": row["outlet_name"],
                "rating": row["rating"],
                "rating_num": row["rating_num"],
                "rating_source": row["rating_source"] if "rating_source" in row.keys() else None,
            }
    return None


def tag_untagged_sources(cur):
    """Fills story_sources.bias_rating for rows dedupe.py left NULL.

    Stores the canonical category ('left'..'right') or the literal 'unrated' -- NULL
    would mean "not looked at yet", so unrated still needs a marker to tell the two
    apart. 'unrated' is an internal bookkeeping value and is never displayed.
    """
    cur.execute("SELECT id, domain, outlet_name FROM story_sources WHERE bias_rating IS NULL")
    rows = cur.fetchall()
    for row in rows:
        bias = get_bias_for_domain(cur, row["domain"])
        rating = bias["rating"] if bias else "unrated"
        # Backfill a human outlet name while we're here -- GDELT often gives us none,
        # and the digest's source links need something better than a bare domain.
        if bias and bias.get("outlet_name") and not row["outlet_name"]:
            cur.execute(
                "UPDATE story_sources SET bias_rating = ?, outlet_name = ? WHERE id = ?",
                (rating, bias["outlet_name"], row["id"]),
            )
        else:
            cur.execute(
                "UPDATE story_sources SET bias_rating = ? WHERE id = ?", (rating, row["id"])
            )
    return len(rows)


def _upsert(cur, domain, outlet_name, rating, source, prefer_existing_allsides=True):
    if prefer_existing_allsides:
        cur.execute("SELECT rating_source FROM source_bias WHERE domain = ?", (domain,))
        existing = cur.fetchone()
        if existing is not None and existing[0] == "allsides" and source != "allsides":
            return False
    cur.execute(
        """INSERT INTO source_bias (domain, outlet_name, rating, rating_num, rating_source, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(domain) DO UPDATE SET
               outlet_name = excluded.outlet_name,
               rating = excluded.rating,
               rating_num = excluded.rating_num,
               rating_source = excluded.rating_source,
               updated_at = excluded.updated_at""",
        (domain, outlet_name, rating, LEAN_NUMERIC.get(rating), source,
         datetime.now(timezone.utc).isoformat()),
    )
    return True


def refresh_allsides(cur):
    """Seeds the curated AllSides outlets. Returns (seeded, missing_outlet_names)."""
    resp = requests.get(ALLSIDES_CSV_URL, timeout=30)
    resp.raise_for_status()
    by_name = {row["news_source"]: row for row in csv.DictReader(io.StringIO(resp.text))}

    seeded, missing = 0, []
    for domain, outlet_name in DOMAIN_TO_ALLSIDES_NAME.items():
        row = by_name.get(outlet_name)
        if row is None:
            missing.append(outlet_name)
            continue
        rating = (row["rating"] or "").strip().lower()
        if rating not in LEAN_NUMERIC:
            missing.append(outlet_name)
            continue
        _upsert(cur, domain, outlet_name, rating, "allsides", prefer_existing_allsides=False)
        seeded += 1
    return seeded, missing


def refresh_mbfc(cur):
    """Seeds the broad MBFC domain set. Returns (seeded, skipped).

    skipped counts entries whose bias code isn't a political lean (pro-science, fake
    news, conspiracy, satire) -- those are credibility calls and carry no left-right
    position, so they're dropped rather than coerced onto the scale.
    """
    resp = requests.get(MBFC_JSON_URL, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    for alias, target in (data.get("aliases") or {}).items():
        DOMAIN_ALIASES.setdefault(alias.lower(), target.lower())

    seeded = skipped = 0
    for key, entry in (data.get("sources") or {}).items():
        rating = MBFC_CODE_TO_RATING.get((entry or {}).get("b"))
        if rating is None:
            skipped += 1
            continue
        domain = ((entry.get("d") or key) or "").strip().lower()
        if domain.startswith("www."):
            domain = domain[4:]
        if not domain or domain in PLATFORM_DOMAINS:
            skipped += 1
            continue
        if _upsert(cur, domain, entry.get("n") or domain, rating, "mbfc"):
            seeded += 1
    return seeded, skipped


def refresh_bias_data(cur):
    """Monthly refresh: AllSides first, then MBFC fills the gaps without overwriting it.

    Returns (total_seeded, missing_allsides_names) to keep bot_runner's existing
    (seeded, missing) contract intact.
    """
    allsides_seeded, missing = refresh_allsides(cur)
    mbfc_seeded, mbfc_skipped = refresh_mbfc(cur)
    print(f"[bias] AllSides seeded {allsides_seeded}, MBFC seeded {mbfc_seeded} "
          f"({mbfc_skipped} non-lean entries skipped)")
    return allsides_seeded + mbfc_seeded, missing


def lean_label(mean):
    """Weighted-mean position on the -6..+6 scale -> the single label shown per story."""
    if mean <= -4.5:
        return "Left"
    if mean <= -1.5:
        return "Leans Left"
    if mean < 1.5:
        return "Center"
    if mean < 4.5:
        return "Leans Right"
    return "Right"


def coverage_bar(counts, total):
    """Proportional Left/Center/Right bar. Largest-remainder allocation, so the blocks
    always sum to exactly BAR_WIDTH instead of drifting with rounding."""
    if not total:
        return ""
    exact = {b: BAR_WIDTH * counts.get(b, 0) / total for b in BUCKET_ORDER}
    blocks = {b: int(v) for b, v in exact.items()}
    remainder = BAR_WIDTH - sum(blocks.values())
    for bucket in sorted(BUCKET_ORDER, key=lambda b: exact[b] - blocks[b], reverse=True):
        if remainder <= 0:
            break
        blocks[bucket] += 1
        remainder -= 1
    return "".join(BUCKET_BLOCK[b] * blocks[b] for b in BUCKET_ORDER)


def story_coverage(cur, story_id):
    """Coverage breakdown across a story's *rated* sources, or None if none are rated.

    Returns {counts, total, lean, bar, summary_line}. Unrated sources are excluded
    entirely -- from the counts, the bar and the weighted lean -- rather than shown as
    an "Unrated" category. A story whose sources are all unrated returns None, and the
    digest omits the coverage line for it instead of printing an empty bar.

    Counted per independent report, not per article and not per domain. Syndicated
    copies are collapsed first (see sources.py): nine Hearst affiliates running one wire
    story are one voice, and letting them vote nine times would invent a consensus that
    doesn't exist -- the failure mode this whole breakdown is meant to expose.
    """
    from news_alert.sources import independent_sources

    cur.execute(
        """SELECT title, domain, bias_rating FROM story_sources WHERE story_id = ?""",
        (story_id,),
    )
    reps = independent_sources(cur.fetchall())
    ratings = [row["bias_rating"] for row in reps if row["bias_rating"] in LEAN_NUMERIC]
    # One rated voice is not a distribution. Rendering it would paint a full-width
    # single-colour bar and read as "every outlet covering this leans X", which is a
    # stronger claim than one outlet can support.
    if len(ratings) < MIN_RATED_FOR_COVERAGE:
        return None

    counts = {}
    for rating in ratings:
        bucket = LEAN_BUCKET[rating]
        counts[bucket] = counts.get(bucket, 0) + 1

    total = len(ratings)
    mean = sum(LEAN_NUMERIC[r] for r in ratings) / total
    parts = [f"{counts[b]} {b}" for b in BUCKET_ORDER if counts.get(b)]

    return {
        "counts": counts,
        "total": total,
        "lean": lean_label(mean),
        "mean": mean,
        "bar": coverage_bar(counts, total),
        "summary_line": " · ".join(parts),
    }


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from news_alert.db import db_cursor

    with db_cursor() as cur:
        seeded, missing = refresh_bias_data(cur)
        cur.execute("SELECT rating_source, COUNT(*) n FROM source_bias GROUP BY rating_source")
        breakdown = cur.fetchall()

    print(f"\nSeeded {seeded} domains total.")
    for row in breakdown:
        print(f"  {row['rating_source']}: {row['n']}")
    if missing:
        print(f"\n{len(missing)} AllSides outlet name(s) not found in the current CSV: {missing}")
