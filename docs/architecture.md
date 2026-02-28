# Architecture

## Overview
The system has two long-running components:

- Relay daemon (`src/replay-daemon.py`): polls IMAP, filters messages, and relays them via SMTP to recipients in SQLite. It appends a unique unsubscribe link for each recipient, enforces per-message send delay, and respects rank-based priority.
- Web app (`src/webserver.py`): HTTPS Flask server that receives unsubscribe requests and provides a basic admin UI for list management, protected by a static username/password.

Both services read from the same SQLite database.

## Data Flow
1. IMAP poll: the relay daemon connects to IMAP and checks for new messages.
2. Filter: if the message matches the configured sender or is a bounce, it is eligible for processing.
3. Load recipients: the daemon reads active recipients from SQLite, ordered by `rank` (ascending).
4. Send loop: the daemon sends messages via SMTP with per-recipient throttling.
5. Unsubscribe link: each message includes a signed token for the recipient (`e`, `t`, `s` query params) and a `List-Unsubscribe` header.
6. Unsubscribe: the web app looks up the token, marks the recipient as unsubscribed, and records timestamp.
7. Admin UI (`/manage`): authenticated operators can bulk add/unsubscribe and edit existing rows in a table.
8. Bounce handling: delivery status notifications are parsed and the bounced recipient is unsubscribed automatically.

## Database Schema
The schema below is the recommended baseline. The code should align with this.

### `recipients`
- `id` INTEGER PRIMARY KEY
- `email` TEXT UNIQUE NOT NULL
- `name` TEXT
- `rank` INTEGER NOT NULL DEFAULT 100
- `unsubscribed` INTEGER NOT NULL DEFAULT 0
- `token` TEXT NOT NULL
- `created_at` TEXT NOT NULL (ISO-8601)
- `updated_at` TEXT NOT NULL (ISO-8601)
- `unsubscribed_at` TEXT (ISO-8601, nullable)

### `config`
- `key` TEXT PRIMARY KEY
- `value` TEXT NOT NULL

The `config` table stores operational state like `last_processed_at` to avoid reprocessing messages.

### `send_log`
Optional table if you want visibility into deliveries.
- `id` INTEGER PRIMARY KEY
- `recipient_id` INTEGER NOT NULL
- `message_id` TEXT NOT NULL
- `sent_at` TEXT NOT NULL (ISO-8601)
- `status` TEXT NOT NULL
- `error` TEXT

## Config Files
- `config/imap.json` for IMAP polling and filter sender (`filter_recipient`).
- `config/smtp.json` for SMTP relay settings (including `from`).
- `config/relay.json` for poll interval, public base URL, and unsubscribe path.
- `config/db.json` for the SQLite path (`${config}/list.db`).
- `config/web.json` for HTTPS bind, domain, admin credentials, and token secret.
- `config/schema.sql` for initializing the database.

## Unsubscribe Token
- One token per recipient stored in the database.
- The token is treated as a secret and must not be logged.
- The unsubscribe link is appended to each email body.

## Priority Rules
- Lower `rank` values are delivered first.
- Ties are broken by `id` ascending for deterministic ordering.

## Rate Limiting
- The relay daemon sleeps between recipients to avoid SMTP provider throttling.

## Security
- Admin UI uses bcrypt hash stored in `config/web.json`.
- Web app must be served only over HTTPS.
- Keep secrets out of version control.

## Operational Notes
- Run both components under a supervisor (systemd) with log rotation.
- Consider a separate dedicated IMAP mailbox.
- Use consistent, concrete dates in any scheduled operations or incident notes.
- TLS is managed by `init-tls.sh` and renewed via `acme.sh --cron`.
