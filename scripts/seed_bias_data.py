"""One-time + periodic (re-run monthly, or via bot_runner.py's scheduled job) CLI
wrapper around news_alert.bias.refresh_bias_data.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news_alert.bias import refresh_bias_data
from news_alert.db import db_cursor


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    with db_cursor() as cur:
        seeded, missing = refresh_bias_data(cur)

    print(f"Seeded {seeded} domains into source_bias.")
    if missing:
        print(f"{len(missing)} mapped outlet name(s) not found in the current AllSides CSV "
              f"(dataset may have changed): {missing}")


if __name__ == "__main__":
    main()
