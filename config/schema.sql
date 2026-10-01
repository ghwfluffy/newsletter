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

CREATE TABLE IF NOT EXISTS newsletter_messages (
  message_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  uid TEXT,
  uidvalidity TEXT
);

CREATE TABLE IF NOT EXISTS domain_blocks (
  domain TEXT PRIMARY KEY,
  reason TEXT NOT NULL,
  blocked_at TEXT NOT NULL,
  released_at TEXT
);

CREATE TABLE IF NOT EXISTS held_deliveries (
  message_id TEXT NOT NULL,
  recipient_id INTEGER NOT NULL,
  uid TEXT NOT NULL,
  uidvalidity TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (message_id, recipient_id)
);

CREATE TABLE IF NOT EXISTS provider_failure_events (
  queue_id TEXT NOT NULL,
  event_at TEXT NOT NULL,
  recipient TEXT NOT NULL,
  PRIMARY KEY (queue_id, event_at, recipient)
);

CREATE TABLE IF NOT EXISTS domain_tests (
  id INTEGER PRIMARY KEY,
  domain TEXT NOT NULL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL,
  result TEXT NOT NULL,
  message_id TEXT,
  recipient_id INTEGER,
  uid TEXT,
  uidvalidity TEXT,
  test_message_id TEXT,
  submitted_at TEXT
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
