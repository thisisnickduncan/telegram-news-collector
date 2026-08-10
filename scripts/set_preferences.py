"""CLI to view/edit the singleton preferences row.

Examples:
    python scripts/set_preferences.py --show
    python scripts/set_preferences.py --seed-placeholders
    python scripts/set_preferences.py --topics "ai regulation,cybersecurity" --regions "US,Global"
    python scripts/set_preferences.py --max-stories 8
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news_alert.db import db_cursor

PLACEHOLDER_TOPICS = ["ai regulation", "cybersecurity", "hawaii politics"]
PLACEHOLDER_REGIONS = ["US", "Global", "Hawaii"]


def split_list(value):
    if value is None:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def load_current(cur):
    cur.execute("SELECT * FROM preferences WHERE id = 1")
    row = cur.fetchone()
    if row is None:
        return None
    return {
        "telegram_chat_id": row["telegram_chat_id"],
        "topics": json.loads(row["topics"]),
        "regions": json.loads(row["regions"]),
        "keywords": json.loads(row["keywords"]) if row["keywords"] else [],
        "excluded_sources": json.loads(row["excluded_sources"]) if row["excluded_sources"] else [],
        "digest_max_stories": row["digest_max_stories"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--show", action="store_true", help="Print current preferences and exit")
    parser.add_argument("--seed-placeholders", action="store_true", help="Seed placeholder topics/regions from the plan doc (only fills fields not already set)")
    parser.add_argument("--topics", help="Comma-separated list, e.g. 'ai regulation,cybersecurity'")
    parser.add_argument("--regions", help="Comma-separated list, e.g. 'US,Global,Hawaii'")
    parser.add_argument("--keywords", help="Comma-separated list of extra filter terms")
    parser.add_argument("--excluded-sources", help="Comma-separated list of domains to skip")
    parser.add_argument("--max-stories", type=int, help="Cap on stories per digest")
    args = parser.parse_args()

    with db_cursor() as cur:
        current = load_current(cur)

        if args.show:
            if current is None:
                print("No preferences set yet.")
            else:
                print(json.dumps(current, indent=2))
            return

        base = current or {
            "telegram_chat_id": None,
            "topics": [],
            "regions": [],
            "keywords": [],
            "excluded_sources": [],
            "digest_max_stories": 6,
        }

        if args.seed_placeholders:
            if not base["topics"]:
                base["topics"] = PLACEHOLDER_TOPICS
            if not base["regions"]:
                base["regions"] = PLACEHOLDER_REGIONS

        if args.topics is not None:
            base["topics"] = split_list(args.topics)
        if args.regions is not None:
            base["regions"] = split_list(args.regions)
        if args.keywords is not None:
            base["keywords"] = split_list(args.keywords)
        if args.excluded_sources is not None:
            base["excluded_sources"] = split_list(args.excluded_sources)
        if args.max_stories is not None:
            base["digest_max_stories"] = args.max_stories

        if not base["topics"] or not base["regions"]:
            parser.error(
                "topics and regions are required. Pass --topics/--regions, "
                "or use --seed-placeholders to start from example values."
            )

        cur.execute(
            """
            INSERT INTO preferences (id, telegram_chat_id, topics, regions, keywords, excluded_sources, digest_max_stories, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                telegram_chat_id = excluded.telegram_chat_id,
                topics = excluded.topics,
                regions = excluded.regions,
                keywords = excluded.keywords,
                excluded_sources = excluded.excluded_sources,
                digest_max_stories = excluded.digest_max_stories,
                updated_at = excluded.updated_at
            """,
            (
                base["telegram_chat_id"],
                json.dumps(base["topics"]),
                json.dumps(base["regions"]),
                json.dumps(base["keywords"]),
                json.dumps(base["excluded_sources"]),
                base["digest_max_stories"],
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    print("Preferences saved:")
    print(json.dumps(base, indent=2))


if __name__ == "__main__":
    main()
