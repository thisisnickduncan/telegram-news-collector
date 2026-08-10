import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
DB_PATH = os.environ.get("DB_PATH", "./db/news_alert.sqlite3")

# Your local timezone: used both for scheduling digests and for the time shown in
# them. The server runs on UTC, so anything user-facing must be converted explicitly
# -- a naive datetime.now() there renders 8:00pm Pacific as "3:00am".
DISPLAY_TIMEZONE = os.environ.get("DISPLAY_TIMEZONE", "America/Los_Angeles")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = PROJECT_ROOT / "db" / "schema.sql"
