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

CREATE TABLE IF NOT EXISTS subscription_requests (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('subscribe', 'unsubscribe')),
  ip_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER,
  sent_at INTEGER,
  confirmed_at INTEGER,
  claimed_at INTEGER,
  last_attempt_at INTEGER,
  attempts INTEGER NOT NULL DEFAULT 0,
  policy_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS subscription_requests_email ON subscription_requests(email, created_at);
CREATE INDEX IF NOT EXISTS subscription_requests_ip ON subscription_requests(ip_hash, created_at);

CREATE TABLE IF NOT EXISTS subscription_events (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL,
  action TEXT NOT NULL,
  requested_at INTEGER NOT NULL,
  confirmed_at INTEGER NOT NULL,
  policy_version TEXT NOT NULL,
  consent_text TEXT NOT NULL
);
