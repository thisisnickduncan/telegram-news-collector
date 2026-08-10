"""Domain -> AllSides bias rating lookup.

The AllSides CSV (favstats/AllSideR) rates *outlets by name*, not by domain, and its
`url` column is AllSides' own profile slug, not the outlet's actual website -- so there's
no ready-made domain column to key off of. DOMAIN_TO_ALLSIDES_NAME below is a small,
hand-curated bridge for outlets whose domain I'm confident about, cross-checked against
the exact `news_source` strings in the CSV. Anything not in this map is deliberately left
out rather than guessed at (see plan section 5.3 / 11: unknown domain -> "Unrated", don't
guess). This means coverage skews US/English-language -- an honest limitation, not a bug.
"""
import csv
import io
from datetime import datetime, timezone

import requests

ALLSIDES_CSV_URL = "https://raw.githubusercontent.com/favstats/AllSideR/master/data/allsides_data.csv"

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

# AllSides categorical rating -> short code used in the SMS digest (plan section 6).
RATING_SHORT_CODE = {
    "left": "L",
    "left-center": "LL",
    "center": "C",
    "right-center": "LR",
    "right": "R",
}


def short_code(rating):
    return RATING_SHORT_CODE.get(rating, "Unrated")


def get_bias_for_domain(cur, domain):
    """Returns a dict {domain, outlet_name, rating, rating_num} or None if unrated."""
    if not domain:
        return None
    cur.execute("SELECT * FROM source_bias WHERE domain = ?", (domain,))
    row = cur.fetchone()
    if row is None:
        return None
    return {
        "domain": row["domain"],
        "outlet_name": row["outlet_name"],
        "rating": row["rating"],
        "rating_num": row["rating_num"],
    }


def tag_untagged_sources(cur):
    """Fills in story_sources.bias_rating for rows dedupe.py left NULL. Stores the short
    code (L/LL/C/LR/R) or the literal "Unrated" -- NULL afterward would mean "not yet
    processed", so unrated sources still need a non-NULL marker to distinguish the two."""
    cur.execute("SELECT id, domain FROM story_sources WHERE bias_rating IS NULL")
    rows = cur.fetchall()
    for row in rows:
        bias = get_bias_for_domain(cur, row["domain"])
        code = short_code(bias["rating"]) if bias else "Unrated"
        cur.execute("UPDATE story_sources SET bias_rating = ? WHERE id = ?", (code, row["id"]))
    return len(rows)


def refresh_bias_data(cur):
    """Re-run monthly (plan section 5.3 / 8): pull the AllSides CSV and populate
    source_bias for every domain in DOMAIN_TO_ALLSIDES_NAME. Returns (seeded_count,
    missing_outlet_names) -- a non-empty missing list means the CSV's outlet naming
    has drifted, not that anything crashed.
    """
    resp = requests.get(ALLSIDES_CSV_URL, timeout=30)
    resp.raise_for_status()
    allsides_by_name = {row["news_source"]: row for row in csv.DictReader(io.StringIO(resp.text))}
    now = datetime.now(timezone.utc).isoformat()

    seeded, missing = 0, []
    for domain, outlet_name in DOMAIN_TO_ALLSIDES_NAME.items():
        row = allsides_by_name.get(outlet_name)
        if row is None:
            missing.append(outlet_name)
            continue

        rating_num = float(row["rating_num"]) if row["rating_num"] not in ("NA", "") else None
        cur.execute(
            """INSERT INTO source_bias (domain, outlet_name, rating, rating_num, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(domain) DO UPDATE SET
                   outlet_name = excluded.outlet_name,
                   rating = excluded.rating,
                   rating_num = excluded.rating_num,
                   updated_at = excluded.updated_at""",
            (domain, outlet_name, row["rating"], rating_num, now),
        )
        seeded += 1

    return seeded, missing


def story_bias_spread(cur, story_id):
    """Distinct short codes (e.g. ['L', 'C', 'R']) across a story's sources, in a
    stable L->R display order. Sources with no bias_rating recorded are ignored here --
    an all-Unrated story just gets an empty spread, which digest.py renders as no tag."""
    cur.execute(
        "SELECT DISTINCT bias_rating FROM story_sources WHERE story_id = ? AND bias_rating IS NOT NULL",
        (story_id,),
    )
    codes = {row["bias_rating"] for row in cur.fetchall()}
    order = ["L", "LL", "C", "LR", "R"]
    return [c for c in order if c in codes]
