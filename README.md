# Newsletter Relay

A lightweight newsletter system. It combines:

- A Python relay daemon that polls IMAP, then relays a message to recipients in SQLite using SMTP.
- A Flask web app (HTTPS on port 443) that receives unsubscribe events and provides a list-management UI.

This is designed to be self-hosted and easy to operate on a small server.

## Features
- IMAP polling with sender/recipient filtering.
- SMTP relay with per-recipient unsubscribe links.
- Throttled delivery to avoid SMTP provider rate limits.
- Recipient rank/priority ordering (send higher rank first).
- Unsubscribe endpoint that requires a confirmation click before marking recipients inactive.
- Admin UI (`/manage`) protected by a static username/password (bcrypt hash stored in config).
- Self-contained TLS issuance/renewal via ACME (`acme.sh`).
- Automatic bounce handling to unsubscribe undeliverable addresses.
- Embedded/attached image resizing (fixed width with EXIF orientation correction).
- `+test` recipient tag routes only back to the sender with a `[TEST]` subject prefix.

## How It Works (Short)
1. The relay daemon polls IMAP for new messages.
2. If the message matches the configured sender or is a bounce, it is queued for processing.
3. The daemon loads active recipients from SQLite, sorted by rank, and sends the message via SMTP.
4. For each recipient, a unique unsubscribe link is appended at the bottom of the email body.
5. The Flask server receives unsubscribe requests and marks the recipient as unsubscribed.
6. The admin UI lets an operator bulk add/unsubscribe and edit existing entries.
7. If any recipient local part contains `+test`, the message is relayed only back to the sender with `[TEST]` in the subject.

## Requirements
- Python 3.10+ recommended.
- SQLite (local file).
- An SMTP account and an IMAP mailbox.
- A TLS certificate for HTTPS (port 443).

## Configuration
All config and secrets live in `config/config.json`, split into sections:

### `config/config.json`
```json
{
  "imap": {
    "host": "imap.example.org",
    "port": 993,
    "username": "newsletter@example.org",
    "password": "...",
    "filter_recipient": "newsletter@example.org"
  },
  "smtp": {
    "host": "smtp.example.org",
    "port": 587,
    "username": "newsletter@example.org",
    "password": "...",
    "from": "Your List <list@example.org>"
  },
  "db": {
    "db_path": "${config}/list.db"
  },
  "web": {
    "bind": "0.0.0.0",
    "port": 443,
    "domain": "listenserver.com",
    "tls_cert": "${config}/tls/${domain}/fullchain.pem",
    "tls_key": "${config}/tls/${domain}/privkey.pem",
    "public_base_url": "https://listenserver.com",
    "confirmation_email_enabled": false,
    "token_secret": "CHANGE_ME_TO_RANDOM_32B+",
    "unsubscribe_path": "/unsub",
    "manage_path": "/manage",
    "admin_user": "admin",
    "admin_pass_bcrypt": "$2b$12$..."
  },
  "relay": {
    "poll_seconds": 30,
    "batch_size": 50,
    "per_recipient_sleep_seconds": [25, 40],
    "per_message_sleep_seconds": [5, 12],
    "between_batches_sleep_seconds": [300, 900]
  },
  "test": {
    "enabled": true,
    "contacts": ["testemail1@example.com"],
    "test_db": "${config}/test.db",
    "filter_recipient": "testsender@example.com"
  }
}
```

## Database
SQLite file path is configurable in both the relay and web app. The expected schema is documented in `docs/architecture.md`.

The web app creates `subscription_requests` and `subscription_events` on startup
if they do not exist. Existing recipient records and subscription status are preserved.

An operator can stage a targeted resend using the SQLite `config` key `pending_replay`.
Its JSON value contains `uid`, `uidvalidity`, `message_id`, `recipient_ids`,
`total_count`, `sent_count`, and `skipped_count`. Stage it while the relay is stopped,
after backing up the database and cancelling the matching old SMTP queue entries.
Starting the relay regenerates only those recipients' messages with the current code,
including messages older than the normal 15-minute cutoff. It preserves the normal
IMAP checkpoint, respects current unsubscribe status, and removes each recipient
from the request after SMTP acceptance. Failed submissions remain pending. Delete
the key to cancel a staged resend. SMTP acceptance does not guarantee final delivery.
As with the normal relay, a crash between SMTP acceptance and the database update
can cause a duplicate submission.

## Setup
Initialize the database:
```bash
./init-db.sh
```

Set the admin password:
```bash
./setpass.sh
```

Initialize TLS via ACME (requires port 80 open):
```bash
./init-tls.sh
```

Notes:
- `setpass.sh` requires `jq`.
- `init-tls.sh` requires `curl` and `jq`.
- `init-dev-tls.sh` creates a self-signed cert for local testing.

## Running (Example)
Run the relay daemon:
```bash
python3 src/replay-daemon.py
```

Run the Flask web app (with TLS):
```bash
python3 src/webserver.py
```

Or run both services with watchdogs:
```bash
./go.sh
```

## Docker Compose Web Deployment
The compose stack runs the web app under Gunicorn, runs the relay daemon as a separate long-running container, and puts Nginx in front of the web app for TLS, ACME HTTP-01 challenges, and static files from `www/`.

The Compose network reserves `172.18.0.0/24`; automatic container addresses use
`172.18.0.128/25`, the relay uses `172.18.0.10`, and the confirmation worker uses
`172.18.0.11`. Ensure this subnet does
not overlap another host network. When using host Postfix through
`host.docker.internal:25`, include `172.18.0.10` and `172.18.0.11` in OpenDKIM's
`InternalHosts` and their `/32` addresses in Postfix's `mynetworks`, alongside localhost. OpenDKIM must sign
the final outgoing message using the domain in the visible From header. Set
Postfix's `milter_default_action = tempfail` so a signing-service outage temporarily
rejects submissions instead of allowing unsigned mail. The relay removes original DKIM/ARC
signatures and authentication results because it changes the message content.
Host Postfix/OpenDKIM configuration and signing keys are managed separately from
Compose; private keys must never be committed.

Nginx proxies `/`, `/privacy`, `/subscribe`, `/unsubscribe`, `/confirm`,
`/request-saved`, `/newsletter-assets/`, and the existing unsubscribe/admin paths
to Gunicorn over HTTPS. Other paths serve static files from `www/` with directory
indexing disabled. Access logs record paths without query strings to avoid logging
email addresses or secret confirmation/unsubscribe links.
The `www/heroes/index.html` page is served at `/heroes` and `/heroes/`. The old typo paths `/heros`, `/cnnheros`, and `/cnnheroes` redirect to `/heroes`.

Existing certificates should live at:
```bash
config/tls/newsmail.spjinc.org/fullchain.pem
config/tls/newsmail.spjinc.org/privkey.pem
```

Start the web stack:
```bash
docker compose up -d --build nginx web relay acme-renew
```

### Public signup and unsubscribe without the newsletter relay

The homepage offers signup and unsubscribe requests. Both send an email link to
verify control of the address; opening a link does not change preferences until
the recipient presses Confirm. New addresses are added to the active list only
after confirmation. Existing subscriptions remain unchanged, including subscriptions
previously requested directly from SPJ. Existing signed newsletter unsubscribe links
continue to work without requiring a second email.

Set `web.confirmation_email_enabled` to `true` to enable the separate outbox worker
(default `false`). It uses the existing SMTP configuration and never processes
newsletters or `pending_replay`. Run only the website and confirmation workflow:

```bash
docker compose --profile confirmations up -d --build web nginx confirmations acme-renew
```

Host Postfix and OpenDKIM must be running to deliver confirmations when using
host SMTP. Keep `relay` stopped to leave staged newsletter replays paused.
Setting `confirmation_email_enabled` to `false` requires recreating web/confirmation
containers to reload configuration; requests can still be saved, and the site
displays a delivery-delay notice. This does not cancel mail already queued in Postfix.

The forms require CSRF protection and apply a honeypot, one request per email per
hour, ten accepted requests per IP per hour, and a global limit of 100 per day.
Responses do not disclose whether an email is on the list. Confirmation links are
valid for 24 hours after preparation. Request records expire after seven days;
confirmed choices are recorded separately with the consent wording and policy version.
The privacy policy at `/privacy` covers this newsletter service and links to SPJ's
contact page for record access, correction, or deletion requests.

If a certificate needs to be issued from scratch, make sure DNS points at this host and ports 80 and 443 are reachable, then run:
```bash
docker compose up -d --build nginx web
ACME_EMAIL=admin@example.org docker compose --profile setup run --rm acme-init
docker compose up -d relay acme-renew
```

The `acme-renew` service runs `acme.sh --cron` every 12 hours and writes renewed certs back into `config/tls/`. The Nginx container reloads itself every 6 hours so renewed cert files are picked up without replacing the container.

Publish to the remote host:
```bash
cp .env.example .env
./publish.sh
```

To reset/bootstrap the remote checkout, preserve existing mounted runtime state (`.env`, `config/config.json`, DB files, ACME state, and TLS files), initialize the database if missing, then run the normal publish flow:
```bash
./first-publish.sh
```

## Operational Notes
- SMTP throttling uses per-recipient sleeps in the relay daemon.
- If `test.enabled` is `true`, the relay sends only to `test.contacts` instead of the subscribed recipients in SQLite.
- If `test.filter_recipient` is set, it overrides `imap.filter_recipient` while test mode is enabled.
- If `test.test_db` is set, it overrides `db.db_path` while test mode is enabled.
- Ranking is a numeric field; lower numbers are sent first (see architecture doc).
- Unsubscribe links are unique per recipient. Treat them as secrets.
- Images larger than 600px wide are resized to 600px wide before sending. Smaller images are left as-is.
- Consider running both services under systemd, with logs written to disk.

## Security Notes
- Store all secrets outside the repo.
- Use HTTPS only for the web app.
- Keep the admin password hash in `config/config.json` under `web.admin_pass_bcrypt` and rotate if needed.

## Documentation
- `docs/architecture.md`

## License
Intended for internal use only. Not liable for anything.
