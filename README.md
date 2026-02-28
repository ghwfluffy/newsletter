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
- Unsubscribe endpoint that marks recipients inactive.
- Admin UI (`/manage`) protected by a static username/password (bcrypt hash stored in config).
- Self-contained TLS issuance/renewal via ACME (`acme.sh`).
- Automatic bounce handling to unsubscribe undeliverable addresses.

## How It Works (Short)
1. The relay daemon polls IMAP for new messages.
2. If the message matches the configured sender or is a bounce, it is queued for processing.
3. The daemon loads active recipients from SQLite, sorted by rank, and sends the message via SMTP.
4. For each recipient, a unique unsubscribe link is appended at the bottom of the email body.
5. The Flask server receives unsubscribe requests and marks the recipient as unsubscribed.
6. The admin UI lets an operator bulk add/unsubscribe and edit existing entries.

## Requirements
- Python 3.10+ recommended.
- SQLite (local file).
- An SMTP account and an IMAP mailbox.
- A TLS certificate for HTTPS (port 443).

## Configuration
All config and secrets are JSON files. Example structure:

### `config/imap.json`
```json
{
  "host": "imap.example.org",
  "port": 993,
  "username": "newsletter@example.org",
  "password": "...",
  "mailbox": "INBOX",
  "filter_recipient": "newsletter@example.org"
}
```

### `config/smtp.json`
```json
{
  "host": "smtp.example.org",
  "port": 587,
  "username": "newsletter@example.org",
  "password": "...",
  "starttls": true,
  "from": "Your List <list@example.org>"
}
```

### `config/web.json`
```json
{
  "bind": "0.0.0.0",
  "port": 443,
  "domain": "listenserver.com",
  "tls_cert": "${config}/tls/${domain}/fullchain.pem",
  "tls_key": "${config}/tls/${domain}/privkey.pem",
  "token_secret": "CHANGE_ME_TO_RANDOM_32B+",
  "admin_user": "admin",
  "admin_pass_bcrypt": "$2b$12$..."
}
```

### `config/db.json`
```json
{
  "db_path": "${config}/list.db"
}
```

### `config/relay.json`
```json
{
  "poll_seconds": 30,
  "public_base_url": "https://listenserver.com",
  "unsubscribe_path": "/unsub"
}
```

## Database
SQLite file path is configurable in both the relay and web app. The expected schema is documented in `docs/architecture.md`.

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

## Operational Notes
- SMTP throttling uses per-recipient sleeps in the relay daemon.
- Ranking is a numeric field; lower numbers are sent first (see architecture doc).
- Unsubscribe links are unique per recipient. Treat them as secrets.
- Consider running both services under systemd, with logs written to disk.

## Security Notes
- Store all secrets outside the repo.
- Use HTTPS only for the web app.
- Keep the admin password hash in `config/web.json` and rotate if needed.

## Documentation
- `docs/architecture.md`

## License
Intended for internal use only. Not liable for anything.
