-- Singleton row, your preferences + bot identity
CREATE TABLE IF NOT EXISTS preferences (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    telegram_chat_id TEXT,            -- captured once, when you /start the bot (register_bot.py)
    topics TEXT NOT NULL,             -- JSON array, e.g. ["ai regulation","cybersecurity","your town politics"]
    regions TEXT NOT NULL,            -- JSON array, e.g. ["US","Global","Your Town"]
    keywords TEXT,                    -- JSON array, extra filter terms
    excluded_sources TEXT,            -- JSON array of domains to skip
    digest_max_stories INTEGER DEFAULT 6,
    pending_ask_story_id INTEGER REFERENCES stories(id),  -- set when you tap "Ask" on a story;
                                                            -- your next free-text message answers against it
    updated_at TEXT
);

-- Bias lookup table, seeded from AllSides dataset
CREATE TABLE IF NOT EXISTS source_bias (
    domain TEXT PRIMARY KEY,          -- e.g. "reuters.com"
    outlet_name TEXT,
    rating TEXT,                      -- Left / Lean Left / Center / Lean Right / Right / Unrated
    rating_num REAL,                  -- -6.0 to +6.0, AllSides numeric scale
    updated_at TEXT
);

-- One row per tracked story (a cluster of articles about the same event)
CREATE TABLE IF NOT EXISTS stories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_key TEXT UNIQUE,          -- normalized headline hash, used for dedup matching
    headline TEXT,
    summary TEXT,
    region TEXT,
    topic TEXT,
    status TEXT DEFAULT 'active',     -- active / followed / muted / expired
    telegram_message_id TEXT,         -- lets us edit the message after a button press
    first_seen_at TEXT,
    last_updated_at TEXT
);

-- Every article that fed into a story, with its bias rating
CREATE TABLE IF NOT EXISTS story_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id INTEGER REFERENCES stories(id),
    title TEXT,                       -- this source's own headline, used as summarizer input
    url TEXT,
    domain TEXT,
    outlet_name TEXT,
    bias_rating TEXT,
    published_at TEXT,
    fetched_at TEXT
);

-- Log of what you were sent, for dedup and debugging
CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at TEXT,
    story_ids TEXT                    -- JSON array
);

CREATE TABLE IF NOT EXISTS follows (
    story_id INTEGER PRIMARY KEY REFERENCES stories(id),
    followed_at TEXT
);
