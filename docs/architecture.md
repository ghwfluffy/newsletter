# Architecture

## Overview
The system has two long-running components:

- Relay daemon (`src/replay-daemon.py`): polls IMAP, filters messages, and relays them via SMTP to recipients in SQLite. It appends a unique unsubscribe link for each recipient, enforces per-message send delay, respects rank-based priority, and resizes embedded/attached images to a fixed width. In the Docker Compose deployment, it runs as the `relay` service using the same image and mounted `config/` directory as the web app.
- Web app (`src/webserver.py`): Flask WSGI app that receives unsubscribe requests and provides a basic admin UI for list management, protected by a static username/password. In the Docker Compose deployment, Gunicorn runs the WSGI app and Nginx handles HTTPS ingress.

Both services read from the same SQLite database.

The optional `confirmations` Compose service runs `src/subscription-mailer.py`.
It handles only requested signup/unsubscribe confirmation emails and can run while
the newsletter relay is stopped. The public homepage and `/privacy` are Flask
templates. Nginx proxies these routes over HTTPS; other static content remains in `www/`.

## Data Flow
1. IMAP poll: the relay daemon connects to IMAP and checks for new messages.
2. Filter: if the message matches the configured sender, it is eligible for processing.
3. Load recipients: the daemon reads active recipients from SQLite, ordered by `rank` (ascending).
4. Send loop: the daemon sends messages via SMTP with per-recipient throttling. Inline and attached images are resized to a fixed width if larger, with EXIF orientation correction.
5. Unsubscribe link: each message includes a signed token for the recipient (`e`, `t`, `s` query params) and a `List-Unsubscribe` header.
6. Unsubscribe: the web app displays a confirmation page, then on confirm marks the recipient as unsubscribed and records timestamp.
7. Admin UI (`/manage`): authenticated operators can bulk add/unsubscribe and edit existing rows in a table.
8. Bounce handling: when `relay.postfix_log_dir` is enabled, trusted local Postfix records for newsletter messages automatically unsubscribe permanently nonexistent or disabled mailboxes. Temporary and policy failures preserve subscription status.
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

### `newsletter_messages`

- `message_id` TEXT PRIMARY KEY: original newsletter source Message-ID.
- `created_at` TEXT NOT NULL: first recorded timestamp in UTC.

The relay creates this table automatically and records normal newsletter and
replay source identities. Bounce handling correlates Postfix cleanup Message-ID
and qmgr envelope sender with the smtp queue ID and recipient. Pending replay
source identity is also recognized, including previously submitted copies.

`relay.postfix_log_dir` defaults to `""` (disabled). Set it to
`/app/postfix-logs` for Compose, which mounts host `/var/log` read-only into
the relay. Only ISO 8601 records in `mail.log.1` and `mail.log` are read.
The files are scanned at most once per minute before polling and between
recipients, with checks delayed during batch sleeps. A matching `status=bounced`
with `5.1.1` or `5.2.1`, or Yahoo/AT&T's `5.0.0` diagnostic
"This mailbox is disabled (554.30)", marks an active recipient unsubscribed
and updates timestamps. Policy, spam, authentication, temporary, full-mailbox,
and unrecognized failures do not unsubscribe. Events older than the recipient's
latest update are ignored, preventing repeated processing from reversing a new
subscription. The scanner uses aggregate logs and catches errors without
exposing addresses. Returned email DSNs and failures missing from these retained
local logs require operator review; no IMAP bounce parser is used.

### Public subscription records

`subscription_requests` is a short-lived outbox with a random request ID, email,
action (`subscribe` or `unsubscribe`), keyed hash of the requesting IP, request and
expiry timestamps, worker claim/retry timestamps, SMTP acceptance timestamp,
confirmation timestamp, attempt count, and policy version. Links are HMAC-signed
using `web.token_secret`; raw bearer tokens are not stored. A worker claim prevents
simultaneous sends by multiple worker instances. SMTP failures retry after ten minutes.
A crash after SMTP acceptance but before saving the result can send a duplicate
confirmation, which cannot activate a subscription without the recipient's confirmation.

`subscription_events` records the email, action, request/confirmation timestamps,
policy version, and consent wording for confirmed web changes. The app creates these
tables if missing without modifying existing recipient status. A pending signup is
not an active recipient; GET requests never change subscription state. Confirmation
POSTs update recipients and consent history in one transaction. Unsubscribe links
already included in newsletters retain their existing behavior.

The service prunes request records older than seven days during routine processing.
Subscription and suppression records and confirmation history remain for newsletter
administration. Access/correction/deletion requests use SPJ's contact page.

## Config Files
- `config/config.json` is the single runtime config file.
- The `imap` section stores IMAP polling settings and the sender filter (`filter_recipient`, string).
- The `smtp` section stores SMTP relay settings, including `from`.
- The `relay` section stores poll interval and relay throttling delays.
- The `db` section stores the SQLite path (`${config}/list.db`).
- The `web` section stores HTTPS bind, domain, public base URL, admin credentials, token secret, unsubscribe path, and manage path.
- `web.confirmation_email_enabled` defaults to `false`. When enabled, the separately
  started `confirmations` worker uses the existing SMTP settings to deliver form
  confirmation emails. It does not read `pending_replay` or the IMAP mailbox.
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
- The Compose relay uses the fixed address `172.18.0.10` and the confirmation worker
  uses `172.18.0.11` on `172.18.0.0/24`;
  dynamic addresses are restricted to `172.18.0.128/25`. Host Postfix permits this
  two sender addresses, and OpenDKIM includes them in `InternalHosts` to select signing
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
- Public forms use secure session cookies, CSRF tokens, validation, rate limits,
  and a honeypot. Confirmation pages do not expose recipient addresses, use
  `Referrer-Policy: no-referrer`, and disable caching. Nginx and Gunicorn access logs
  exclude query strings so secret links and email addresses are not logged.

## Operational Notes
- Run both components under a supervisor (systemd) with log rotation.
- The Docker Compose deployment proxies the public subscription pages and the
  existing unsubscribe/admin routes to Gunicorn, serves other `www/` content through
  Nginx, disables directory indexing, and stores ACME certificates under
  `config/tls/<domain>/`.
- Consider a separate dedicated IMAP mailbox.
- Use consistent, concrete dates in any scheduled operations or incident notes.
- TLS is managed by `init-tls.sh` and renewed via `acme.sh --cron`.
