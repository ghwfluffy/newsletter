# Architecture

## Overview
The system has two long-running components:

- Relay daemon (`src/replay-daemon.py`): polls IMAP, filters messages, and relays them via SMTP to recipients in SQLite. It appends a unique unsubscribe link for each recipient, enforces per-message send delay, respects rank-based priority, and resizes embedded/attached images to a fixed width. In the Docker Compose deployment, it runs as the `relay` service using the same image and mounted `config/` directory as the web app.
- Web app (`src/webserver.py`): Flask WSGI app that receives unsubscribe requests and provides a basic admin UI for list management, protected by a static username/password. In the Docker Compose deployment, Gunicorn runs the WSGI app and Nginx handles HTTPS ingress.

Both services read from the same SQLite database.

## Data Flow
1. IMAP poll: the relay daemon connects to IMAP and checks for new messages.
2. Filter: if the message matches the configured sender or is a bounce, it is eligible for processing.
3. Load recipients: the daemon reads active recipients from SQLite, ordered by `rank` (ascending).
4. Send loop: the daemon sends messages via SMTP with per-recipient throttling. Inline and attached images are resized to a fixed width if larger, with EXIF orientation correction.
5. Unsubscribe link: each message includes a signed token for the recipient (`e`, `t`, `s` query params) and a `List-Unsubscribe` header.
6. Unsubscribe: the web app displays a confirmation page, then on confirm marks the recipient as unsubscribed and records timestamp.
7. Admin UI (`/manage`): authenticated operators can bulk add/unsubscribe and edit existing rows in a table.
8. Bounce handling: delivery status notifications are parsed and the bounced recipient is unsubscribed automatically.
9. Test tag handling: if any recipient local part contains `+test`, the message is relayed only back to the sender with `[TEST]` in the subject.

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

The `config` table stores operational state like `last_processed_at`, `last_uid`, message status timestamps, message type, and delivery progress counters.

The optional `pending_replay` key stages an operator-selected resend. Its JSON value
contains the source IMAP `uid`, mailbox `uidvalidity`, original `message_id`, ordered
`recipient_ids`, and `total_count`, `sent_count`, and `skipped_count` counters. The
relay processes this request before normal polling, verifies mailbox and message
identity, and bypasses the age cutoff only for this source. It regenerates messages
from the original MIME source and checks each recipient's current unsubscribe status.
Recipients are removed from the request after SMTP acceptance or if no longer active;
failed submissions stay pending, and the key is deleted when the request is exhausted.
Normal IMAP checkpoints are preserved. Staging a request does not start the relay.
Progress measures SMTP acceptance, not destination delivery. A crash between SMTP
acceptance and saving progress can cause a duplicate; the two systems do not share
a transaction.

### `send_log`
Optional table if you want visibility into deliveries.
- `id` INTEGER PRIMARY KEY
- `recipient_id` INTEGER NOT NULL
- `message_id` TEXT NOT NULL
- `sent_at` TEXT NOT NULL (ISO-8601)
- `status` TEXT NOT NULL
- `error` TEXT

## Config Files
- `config/config.json` is the single runtime config file.
- The `imap` section stores IMAP polling settings and the sender filter (`filter_recipient`, string).
- The `smtp` section stores SMTP relay settings, including `from`.
- The `relay` section stores poll interval and relay throttling delays.
- The `db` section stores the SQLite path (`${config}/list.db`).
- The `web` section stores HTTPS bind, domain, public base URL, admin credentials, token secret, unsubscribe path, and manage path.
- The `test` section stores an optional test-mode switch, override recipient list, test-only sender filter override, and test-only DB override.
- `config/schema.sql` initializes the database.

## Unsubscribe Token
- One token per recipient stored in the database.
- The token is treated as a secret and must not be logged.
- The unsubscribe link is appended to each email body.

## Priority Rules
- Lower `rank` values are delivered first.
- Ties are broken by `id` ascending for deterministic ordering.

## Rate Limiting
- The relay daemon sleeps between recipients to avoid SMTP provider throttling.

## Test Mode
- If `test.enabled` is `true`, the relay sends only to the configured `test.contacts` list instead of loading active recipients from SQLite.
- If `test.filter_recipient` is set, it overrides `imap.filter_recipient` while test mode is enabled.
- If `test.test_db` is set, it overrides `db.db_path` while test mode is enabled.
- Test-mode contacts use the non-destructive `"Test"` unsubscribe token.

## Security
- The Compose relay uses the fixed address `172.18.0.10` on `172.18.0.0/24`;
  dynamic addresses are restricted to `172.18.0.128/25`. Host Postfix permits this
  relay address, and OpenDKIM includes it in `InternalHosts` to select signing
  instead of verification. Keep these host settings aligned with Compose.
- The relay removes original DKIM, legacy DomainKey, ARC, and Authentication-Results
  headers before modifying a newsletter. Host OpenDKIM signs the final MIME bytes
  with the sender domain's private key; the matching public key is published in DNS.
  Postfix uses `milter_default_action = tempfail` to defer submissions when the
  signing filter is unavailable. SMTP acceptance still does not guarantee inbox
  delivery or resolve a provider's sending-IP restrictions.
- Admin UI uses bcrypt hash stored in `config/config.json` under `web.admin_pass_bcrypt`.
- Web app must be served only over HTTPS.
- Keep secrets out of version control.

## Operational Notes
- Run both components under a supervisor (systemd) with log rotation.
- The Docker Compose web deployment serves `www/` through Nginx, proxies `/unsub` and `/manage` to Gunicorn, disables directory indexing, and stores ACME-issued certificates under `config/tls/<domain>/`.
- Consider a separate dedicated IMAP mailbox.
- Use consistent, concrete dates in any scheduled operations or incident notes.
- TLS is managed by `init-tls.sh` and renewed via `acme.sh --cron`.
