# AGENTS.md

## Scope
This file describes how to work in this repository. Follow these instructions when making changes.

## Summary
This project provides a newsletter relay daemon and a Flask web app.

- Relay daemon: polls IMAP for a sender/recipient filter and relays messages via SMTP to all active recipients in SQLite. It appends a per-recipient unsubscribe link, throttles delivery, and respects recipient rank/priority.
- Flask app: serves HTTPS on port 443, receives unsubscribe events, and provides a list-management UI with static credentials stored as a bcrypt hash in a config file.

## Repository Layout
- `src/replay-daemon.py` - IMAP polling and SMTP relay daemon.
- `src/webserver.py` - Flask app (unsubscribe endpoint + list management UI).
- `docs/architecture.md` - system architecture and data model.
- `config/` - local config and secrets (not committed). Includes SMTP/IMAP JSON and admin password hash.
- `init-db.sh` - create the SQLite database from `config/schema.sql`.
- `init-tls.sh` - issue/renew TLS via ACME (`acme.sh`).
- `init-dev-tls.sh` - create a self-signed TLS cert for local testing.
- `setpass.sh` - set admin password in `config/web.json`.
- `go.sh` - run relay, web, and TLS watchdogs.

## Conventions
- Prefer ASCII in files unless a non-ASCII character is required.
- Prefer `${var}` format for Bash variables.
- Keep configuration in JSON files under `config/` or repo root (see README).
- SQLite is the source of truth for recipients and unsubscribe status.
- Use explicit, concrete dates when describing schedules or time-based behavior.

## Security and Privacy
- Never log or print full recipient lists or unsubscribe tokens.
- Use per-recipient unsubscribe tokens; treat them as secrets.
- Do not store raw admin passwords; only store bcrypt hashes.
- The admin UI must require authentication and should be served only over HTTPS.

## Testing and Verification
If tests exist, run them. If no tests exist, state that in your response and describe what you validated manually (if anything).

## Editing Notes
- Avoid reformatting files unless necessary.
- If you add a new configuration key, document it in `README.md` and `docs/architecture.md`.
