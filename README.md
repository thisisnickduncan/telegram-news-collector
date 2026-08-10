# telegram-news-collector

A personal news digest bot. It pulls articles from GDELT for topics/regions you
care about, dedupes and clusters them into stories, tags sources with bias
ratings, summarizes each story with Claude, and delivers a digest to you over
Telegram every 4 hours. From the digest you can ask follow-up questions about a
story (answered with live web search) or just message the bot to start
tracking a new topic.

## Features

- **Scheduled digests** — fetch → dedupe → bias-tag → summarize → send, on a
  4-hour cron (00:00/04:00/08:00/12:00/16:00/20:00 Pacific, DST-aware).
- **Region-aware GDELT fetching** — one query per (topic, region) pair against
  the GDELT DOC 2.0 API.
- **Dedupe/clustering** — fuzzy-matches articles across sources into a single
  story so you're not shown the same event five times.
- **Source bias ratings** — outlets are tagged from an AllSides-derived
  dataset, refreshed monthly.
- **Claude-powered summaries** — each story gets a short summary (not just the
  raw headline) for the digest.
- **Interactive Telegram bot** — per-story "Ask" button for follow-up Q&A
  (Claude + web search), and free-text messages are parsed to start tracking
  new topics on the fly.
- **Failure alerts** — if a scheduled run breaks, you get a Telegram message
  instead of a silently dead pipeline.

## Requirements

- Python 3.11+
- A Telegram bot token (create one via [@BotFather](https://t.me/BotFather))
- An [Anthropic API key](https://console.anthropic.com/)

## Setup

```bash
git clone https://github.com/thisisnickduncan/telegram-news-collector.git
cd telegram-news-collector
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS/Linux
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in the values:

```
TELEGRAM_BOT_TOKEN=
ANTHROPIC_API_KEY=
DB_PATH=./db/news_alert.sqlite3
```

Initialize the database:

```bash
python scripts/init_db.py
```

Set your topics/regions (or start from placeholders and edit later):

```bash
python scripts/set_preferences.py --topics "ai regulation,cybersecurity" --regions "US,Global"
# or: python scripts/set_preferences.py --seed-placeholders
```

Seed source bias ratings:

```bash
python scripts/seed_bias_data.py
```

Register the bot with your Telegram chat — message your bot on Telegram
(anything, e.g. `/start`) right after running this, it polls for that message:

```bash
python scripts/register_bot.py
```

## Running

Run the full bot (scheduler + Telegram polling) in one long-lived process:

```bash
python news_alert/bot_runner.py
```

Or run a single pipeline pass manually (fetch → dedupe → bias → summarize →
send), without the scheduler/polling loop:

```bash
python news_alert/pipeline.py
```

## Project structure

```
news_alert/
  fetcher.py           - GDELT DOC 2.0 API pull, region-aware
  dedupe.py             - fuzzy-matches articles into stories
  bias.py               - source bias tagging + monthly refresh
  summarizer.py         - Claude summaries for new stories
  digest.py             - composes the Telegram digest message
  telegram_client.py    - thin wrapper around the Telegram Bot API
  callback_handler.py   - handles inline-button presses (Ask, etc.)
  message_handler.py    - handles free-text messages (Q&A / new topics)
  pipeline.py           - orchestrates one fetch->send run
  bot_runner.py         - long-poll + APScheduler process (the real entrypoint)
  db.py                 - sqlite connection/cursor helpers
  config.py             - env var loading
scripts/
  init_db.py            - create the sqlite schema
  set_preferences.py    - view/edit topics, regions, digest size, etc.
  register_bot.py       - capture your Telegram chat_id
  seed_bias_data.py     - one-off/monthly CLI wrapper around bias.refresh_bias_data
db/
  schema.sql             - sqlite schema
deploy/
  news-alert.service           - systemd unit (production)
  journald-news-alert.conf     - journald log retention/size caps
```

## Deployment

In production this runs as a systemd service (`deploy/news-alert.service`) on
a small always-on VM rather than locally — `bot_runner.py` is the single
authoritative Telegram poller, so only one instance should ever run against a
given bot token. `deploy/journald-news-alert.conf` caps journald's disk usage
for the service's logs.

## Notes

- GDELT rate-limits aggressively; `fetcher.py` paces requests (15s between
  calls, with backoff) to stay under that in practice.
- Region matching uses FIPS 10-4 country codes (GDELT's `sourcecountry:`
  operator), not ISO codes — a deliberately incomplete country map lives in
  `fetcher.py`; anything not in it (e.g. a US state) is folded in as a plain
  keyword instead of guessed at.
