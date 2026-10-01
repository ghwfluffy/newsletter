"""Persistent provider holds shared by the relay and authenticated admin UI."""
from datetime import datetime, timezone
import re
import sqlite3


SCHEMA = """
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
"""


def ensure_schema(con: sqlite3.Connection) -> None:
    # Do not use executescript here: it commits an existing transaction.
    for statement in SCHEMA.split(';'):
        if statement.strip():
            con.execute(statement)
    con.execute('CREATE TABLE IF NOT EXISTS newsletter_messages '
                '(message_id TEXT PRIMARY KEY, created_at TEXT NOT NULL)')
    columns = {row[1] for row in con.execute('PRAGMA table_info(newsletter_messages)')}
    for column in ('uid', 'uidvalidity'):
        if column not in columns:
            con.execute(f'ALTER TABLE newsletter_messages ADD COLUMN {column} TEXT')


def domain_for(address: str) -> str:
    return address.rsplit('@', 1)[-1].strip().lower()


def valid_domain(domain: str) -> bool:
    return len(domain) <= 253 and bool(re.fullmatch(
        r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+'
        r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', domain
    ))


def is_blocked(con: sqlite3.Connection, address: str) -> bool:
    return con.execute('SELECT 1 FROM domain_blocks WHERE domain=? AND released_at IS NULL',
                       (domain_for(address),)).fetchone() is not None


def block_domain(con: sqlite3.Connection, domain: str, reason: str,
                 event_at: str | None = None) -> None:
    domain = domain.strip().lower()
    if not valid_domain(domain):
        raise ValueError('Invalid email domain')
    now = event_at or datetime.now(timezone.utc).isoformat()
    con.execute(
        'INSERT INTO domain_blocks(domain,reason,blocked_at,released_at) VALUES (?,?,?,NULL) '
        'ON CONFLICT(domain) DO UPDATE SET reason=excluded.reason, '
        'blocked_at=excluded.blocked_at,released_at=NULL '
        'WHERE domain_blocks.released_at IS NULL OR domain_blocks.released_at < excluded.blocked_at',
        (domain, reason, now),
    )


def hold_delivery(con: sqlite3.Connection, source: dict, recipient_id: int,
                  event_at: str | None = None) -> None:
    if not source.get('uid') or not source.get('uidvalidity'):
        raise ValueError('Missing source mailbox identity for retry')
    con.execute(
        'INSERT INTO held_deliveries VALUES (?,?,?,?,?) '
        'ON CONFLICT(message_id,recipient_id) DO UPDATE SET '
        'created_at=MAX(held_deliveries.created_at,excluded.created_at)',
        (source['message_id'], recipient_id, str(source['uid']), str(source['uidvalidity']),
         event_at or datetime.now(timezone.utc).isoformat()),
    )


def policy_failure(dsn: str, diagnostic: str) -> bool:
    if not dsn.startswith(('4.', '5.')):
        return False
    return bool(re.search(
        r'\b(spam|block(?:ed|list)?|blacklist(?:ed)?|reputation|'
        r'policy|SPF|DKIM|DMARC|unsolicited|complaints|abuse|'
        r'authentication|unauthenticated|TSS\d+)\b', diagnostic, re.IGNORECASE
    ))
