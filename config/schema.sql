CREATE TABLE IF NOT EXISTS recipients (
  id INTEGER PRIMARY KEY,
  email TEXT UNIQUE NOT NULL,
  name TEXT,
  rank INTEGER NOT NULL DEFAULT 100,
  unsubscribed INTEGER NOT NULL DEFAULT 0,
  token TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  unsubscribed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_recipients_rank ON recipients (rank, id);
CREATE INDEX IF NOT EXISTS idx_recipients_unsubscribed ON recipients (unsubscribed);

CREATE TABLE IF NOT EXISTS config (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
