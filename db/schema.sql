-- Fresh-install schema. Existing databases are brought here by scripts/migrate_*.py,
-- which run in order and are each idempotent -- this file is the destination, not a
-- second source of truth, so anything added by a migration belongs here too or a new
-- install and an upgraded one stop matching.

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
    pending_why_rule_id INTEGER REFERENCES mute_rules(id),  -- set when you mute a category and
                                                            -- we ask why; your next message is the reason
    pending_why_at TEXT,              -- when that question was asked, so it can lapse
    updated_at TEXT
);

-- Bias lookup table, seeded from the AllSides and MBFC datasets
CREATE TABLE IF NOT EXISTS source_bias (
    domain TEXT PRIMARY KEY,          -- e.g. "reuters.com"
    outlet_name TEXT,
    rating TEXT,                      -- Left / Lean Left / Center / Lean Right / Right / Unrated
    rating_num REAL,                  -- -6.0 to +6.0, AllSides numeric scale
    rating_source TEXT,               -- 'allsides' or 'mbfc'; AllSides wins on conflict
    updated_at TEXT
);

-- One row per tracked story (a cluster of articles about the same event)
CREATE TABLE IF NOT EXISTS stories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_key TEXT UNIQUE,          -- normalized headline hash, used for dedup matching
    headline TEXT,
    summary TEXT,
    expanded_summary TEXT,            -- deeper write-up, produced when you ask for more
    region TEXT,
    topic TEXT,
    status TEXT DEFAULT 'active',     -- active / followed / muted / duplicate / expired
    duplicate_of INTEGER REFERENCES stories(id),  -- the story this turned out to repeat
    telegram_message_id TEXT,         -- lets us edit the message after a button press
    delivered_at TEXT,                -- when it was first sent to you; NULL = never sent
    delivered_source_count INTEGER,   -- how many independent voices it had when sent
    development_summary TEXT,         -- the last development reported after that send,
    development_at TEXT,              -- and when -- the watermark for the next check
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

-- Categories you've asked to stop seeing, in plain English
CREATE TABLE IF NOT EXISTS mute_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule TEXT NOT NULL,               -- e.g. "lottery jackpots and prize drawings"
    source_story_id INTEGER REFERENCES stories(id),
    source_headline TEXT,             -- what you were looking at when you created it
    reason TEXT,                      -- your own words for why, when you answered "why?"
    created_at TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

-- Log of what you were sent; the last row is what a later reply resolves against
CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at TEXT,
    story_ids TEXT                    -- JSON array
);

CREATE TABLE IF NOT EXISTS follows (
    story_id INTEGER PRIMARY KEY REFERENCES stories(id),
    followed_at TEXT
);

-- Kept for history. Deep-dive requests are answered on the spot now, not queued.
CREATE TABLE IF NOT EXISTS deep_dive_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id INTEGER REFERENCES stories(id),
    requested_at TEXT,
    consumed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_stories_status ON stories(status);
CREATE INDEX IF NOT EXISTS idx_stories_delivered ON stories(delivered_at);
CREATE INDEX IF NOT EXISTS idx_story_sources_story ON story_sources(story_id);
CREATE INDEX IF NOT EXISTS idx_story_sources_fetched ON story_sources(story_id, fetched_at);
CREATE INDEX IF NOT EXISTS idx_mute_rules_active ON mute_rules(active) WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_deep_dive_pending ON deep_dive_requests(consumed_at, story_id);
