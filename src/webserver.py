#!/usr/bin/env python3

from flask import Flask, request, abort, session
import sqlite3
import hmac
import hashlib
import base64
import secrets
import re
import html
from datetime import datetime, timezone
import bcrypt
from config import load_config
from subscriptions import register_public_routes
from delivery_state import ensure_schema, block_domain, valid_domain
from html import escape


app_config = load_config()

app = Flask(__name__)
register_public_routes(app, app_config)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_auth() -> bool:
    if not app_config.web.admin_user or not app_config.web.admin_pass_bcrypt:
        abort(500, description="Admin credentials not configured.")
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
    except Exception:
        return False
    if ":" not in decoded:
        return False
    user, password = decoded.split(":", 1)
    if user != app_config.web.admin_user:
        return False
    return bcrypt.checkpw(
        password.encode("utf-8"), app_config.web.admin_pass_bcrypt.encode("utf-8")
    )


def _auth_challenge():
    return ("Authentication required\n", 401, {"WWW-Authenticate": "Basic realm=\"Newsletter\""})


def _split_emails(blob: str) -> list[str]:
    if not blob:
        return []
    parts = re.split(r"[,\n\t:;]+", blob)
    out = []
    for p in parts:
        e = p.strip().lower()
        if e:
            out.append(e)
    return out


def _get_conn():
    con = sqlite3.connect(app_config.resolved_db_path)
    ensure_schema(con)
    con.commit()
    return con


def _get_config_value(cur, key: str) -> str | None:
    row = cur.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _render_timestamp_cell(value: str) -> str:
    if value == "Never":
        return "Never"
    escaped = html.escape(value, quote=True)
    return f'<span class="js-local-ts" data-iso="{escaped}">{escaped}</span>'


def _upsert_recipient(cur, email: str, rank: int | None, subscribed: bool | None, name: str | None):
    now = _now_iso()
    row = cur.execute("SELECT id, token FROM recipients WHERE email=?", (email,)).fetchone()
    if row:
        unsubscribed = None
        if subscribed is not None:
            unsubscribed = 0 if subscribed else 1
        if unsubscribed is None:
            cur.execute(
                "UPDATE recipients SET rank=COALESCE(?, rank), name=COALESCE(?, name), updated_at=? WHERE email=?",
                (rank, name, now, email),
            )
        else:
            unsub_at = now if unsubscribed == 1 else None
            cur.execute(
                "UPDATE recipients SET rank=COALESCE(?, rank), name=COALESCE(?, name), unsubscribed=?, unsubscribed_at=?, updated_at=? WHERE email=?",
                (rank, name, unsubscribed, unsub_at, now, email),
            )
        return

    token = secrets.token_hex(16)
    unsubscribed = 0 if subscribed is None or subscribed else 1
    unsub_at = now if unsubscribed == 1 else None
    cur.execute(
        "INSERT INTO recipients (email, name, rank, unsubscribed, token, created_at, updated_at, unsubscribed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (email, name, rank if rank is not None else 100, unsubscribed, token, now, now, unsub_at),
    )


def sign(email_addr: str, token: str) -> str:
    msg = f"{email_addr}\n{token}".encode("utf-8")
    return hmac.new(app_config.web.token_secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def unsub():
    if request.method == "POST":
        e = (request.form.get("e") or "").lower()
        t = request.form.get("t") or ""
        s = request.form.get("s") or ""
    else:
        e = (request.args.get("e") or "").lower()
        t = request.args.get("t") or ""
        s = request.args.get("s") or ""

    if not e or not t or not s:
        abort(400)

    if t == "Test":
        return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Confirm Unsubscribe</title></head>
<body>
  <h1>Test Unsubscribe</h1>
  <p>This is a test. The unsubscribe page looks like this though.</p>
  <form method="post" action="{app_config.web.unsubscribe_path}">
    <input type="hidden" name="e" value="{e}">
    <input type="hidden" name="t" value="{t}">
    <input type="hidden" name="s" value="{s}">
    <button type="submit">Confirm Unsubscribe</button>
  </form>
</body>
</html>
"""

    if not hmac.compare_digest(sign(e, t), s):
        abort(403)

    con = _get_conn()
    cur = con.cursor()
    row = cur.execute("SELECT token, unsubscribed FROM recipients WHERE email=?", (e,)).fetchone()
    if not row or row[0] != t:
        abort(403)

    if request.method == "GET":
        return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Confirm Unsubscribe</title></head>
<body>
  <h1>Confirm Unsubscribe</h1>
  <p>Click confirm to stop receiving these emails.</p>
  <form method="post" action="{app_config.web.unsubscribe_path}">
    <input type="hidden" name="e" value="{e}">
    <input type="hidden" name="t" value="{t}">
    <input type="hidden" name="s" value="{s}">
    <button type="submit">Confirm Unsubscribe</button>
  </form>
</body>
</html>
"""

    if row[1] == 1:
        return "You are already unsubscribed.\n", 200

    now = _now_iso()
    cur.execute(
        "UPDATE recipients SET unsubscribed=1, unsubscribed_at=?, updated_at=? WHERE email=?",
        (now, now, e),
    )
    con.commit()
    return "Unsubscribed. You will no longer receive these emails.\n", 200


def manage():
    if not _require_auth():
        return _auth_challenge()

    message = ""
    if "domain_csrf" not in session:
        session["domain_csrf"] = secrets.token_urlsafe(32)
    domain_csrf = session["domain_csrf"]
    if request.method == "POST":
        action = request.form.get("action", "")
        if action in {"block_domain", "release_domain", "test_domain"}:
            if not hmac.compare_digest(request.form.get("domain_csrf", ""), domain_csrf):
                abort(403)
        con = _get_conn()
        cur = con.cursor()

        if action in {"block_domain", "release_domain", "test_domain"}:
            domain = request.form.get("domain", "").strip().lower()
            if not valid_domain(domain):
                con.close()
                abort(400, description="Enter an email domain such as hotmail.com.")
            if action == "block_domain":
                block_domain(con, domain, "Manually held by an administrator")
                message = f"Delivery to {domain} is held."
            elif action == "release_domain":
                cur.execute("UPDATE domain_blocks SET released_at=? WHERE domain=?",
                            (_now_iso(), domain))
                message = f"Hold removed for {domain}. Queued messages will retry automatically."
            else:
                if not cur.execute("SELECT 1 FROM domain_blocks WHERE domain=? AND released_at IS NULL", (domain,)).fetchone():
                    con.close()
                    abort(400, description="Hold the domain before requesting a test.")
                if cur.execute("SELECT 1 FROM domain_tests WHERE domain=? "
                               "AND status IN ('pending','sending','submitted')", (domain,)).fetchone():
                    message = f"A test for {domain} is already pending."
                else:
                    cur.execute("INSERT INTO domain_tests(domain,created_at,status,result) VALUES (?,?,'pending',?)",
                                (domain, _now_iso(), "Waiting for the relay to test one queued message. Domain remains held."))
                    message = f"One queued message will be tested for {domain}. Refresh to see the result; the domain remains held."

        if action in {"bulk_subscribe", "bulk_unsubscribe"}:
            bulk_input = request.form.get("bulk_input", "")
            for email in _split_emails(bulk_input):
                _upsert_recipient(cur, email, None, action == "bulk_subscribe", None)
            message = "Bulk update complete."

        if action == "save_existing":
            ids = request.form.getlist("row_id")
            for row_id in ids:
                email = (request.form.get(f"email_{row_id}") or "").strip().lower()
                name = (request.form.get(f"name_{row_id}") or "").strip() or None
                rank_raw = (request.form.get(f"rank_{row_id}") or "").strip()
                unsub_raw = (request.form.get(f"unsub_{row_id}") or "0").strip()
                if not email:
                    continue
                try:
                    rank = int(rank_raw)
                except ValueError:
                    rank = 100
                unsubscribed = 1 if unsub_raw == "1" else 0
                now = _now_iso()
                if unsubscribed == 1:
                    cur.execute(
                        "UPDATE recipients SET email=?, name=?, rank=?, unsubscribed=1, "
                        "unsubscribed_at=COALESCE(unsubscribed_at, ?), updated_at=? WHERE id=?",
                        (email, name, rank, now, now, row_id),
                    )
                else:
                    cur.execute(
                        "UPDATE recipients SET email=?, name=?, rank=?, unsubscribed=0, "
                        "unsubscribed_at=NULL, updated_at=? WHERE id=?",
                        (email, name, rank, now, row_id),
                    )
            message = "Saved existing entries."

        con.commit()
        con.close()

    con = _get_conn()
    cur = con.cursor()
    last_message_received_at = _get_config_value(cur, "last_message_received_at") or "Never"
    last_handled_message_received_at = _get_config_value(cur, "last_handled_message_received_at") or "Never"
    last_handled_message_type = _get_config_value(cur, "last_handled_message_type") or "Never"
    last_delivery_sent_count = _get_config_value(cur, "last_delivery_sent_count") or "0"
    last_delivery_total_count = _get_config_value(cur, "last_delivery_total_count") or "0"
    rows = cur.execute(
        "SELECT id, email, rank, unsubscribed, name FROM recipients ORDER BY email ASC"
    ).fetchall()
    domain_rows = cur.execute(
        "SELECT b.domain,b.reason,b.blocked_at,COUNT(h.recipient_id) FROM domain_blocks b "
        "LEFT JOIN recipients r ON lower(substr(r.email,instr(r.email,'@')+1))=b.domain "
        "AND r.unsubscribed=0 LEFT JOIN held_deliveries h ON h.recipient_id=r.id "
        "WHERE b.released_at IS NULL GROUP BY b.domain ORDER BY b.domain"
    ).fetchall()
    held_total = cur.execute("SELECT COUNT(*) FROM held_deliveries h JOIN recipients r "
                             "ON r.id=h.recipient_id WHERE r.unsubscribed=0").fetchone()[0]
    test_rows = {row[0]: row[1:] for row in cur.execute(
        "SELECT domain,status,result,created_at FROM domain_tests "
        "WHERE id IN (SELECT MAX(id) FROM domain_tests GROUP BY domain)"
    )}
    replay = _get_config_value(cur, "pending_replay")
    queued_blocked = 0
    if replay:
        import json
        queued_ids = set(json.loads(replay)["recipient_ids"])
        blocked_domains = {row[0] for row in domain_rows}
        queued_blocked = sum(rid in queued_ids and not unsub and email.rsplit("@", 1)[-1].lower() in blocked_domains
                             for rid, email, _rank, unsub, _name in rows)
    con.close()

    mode = "Debug" if app_config.test.enabled else "Production"
    last_delivery_progress = f"{last_delivery_sent_count}/{last_delivery_total_count}"
    last_message_received_html = _render_timestamp_cell(last_message_received_at)
    last_handled_message_received_html = _render_timestamp_cell(last_handled_message_received_at)

    table_rows = []
    for rid, email, rank, unsub, name in rows:
        status_unsub = "selected" if unsub else ""
        status_sub = "selected" if not unsub else ""
        name = name or ""
        table_rows.append(
            f"""
      <tr>
        <td>
          <input type="hidden" name="row_id" value="{rid}" />
          <input name="email_{rid}" value="{email}" />
        </td>
        <td><input name="name_{rid}" value="{name}" /></td>
        <td><input name="rank_{rid}" value="{rank}" size="4" /></td>
        <td>
          <select name="unsub_{rid}">
            <option value="0" {status_sub}>Subscribed</option>
            <option value="1" {status_unsub}>Unsubscribed</option>
          </select>
        </td>
      </tr>
"""
        )
    table_html = "".join(table_rows) if table_rows else "<tr><td colspan=\"4\">No entries.</td></tr>"
    domain_html = "".join(
        f'<tr><td><strong>{escape(domain)}</strong></td><td>{count}</td>'
        f'<td>{escape(reason)}</td><td>{_render_timestamp_cell(blocked_at)}</td>'
        f'<td>{escape(test_rows.get(domain, ("", "No test requested.", ""))[1])}</td>'
        f'<td><form method="post"><input type="hidden" name="domain_csrf" value="{domain_csrf}">'
        f'<input type="hidden" name="domain" value="{escape(domain, quote=True)}">'
        '<button type="submit" name="action" value="release_domain">Remove hold and retry</button>'
        '</form><form method="post">'
        f'<input type="hidden" name="domain_csrf" value="{domain_csrf}">'
        f'<input type="hidden" name="domain" value="{escape(domain, quote=True)}">'
        '<button type="submit" name="action" value="test_domain">Test one queued message</button>'
        '</form></td></tr>' for domain, reason, blocked_at, count in domain_rows
    ) or '<tr><td colspan="6">No domains are held.</td></tr>'

    html = f"""
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Manage Newsletter</title>
    <style>
      body {{ font-family: sans-serif; margin: 24px; background: #f7f7f7; }}
      textarea {{ width: 100%; min-height: 140px; }}
      .small {{ font-size: 12px; color: #555; }}
      .section {{ margin-bottom: 24px; }}
      .card {{ background: #fff; padding: 16px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
      table {{ width: 100%; border-collapse: collapse; }}
      th, td {{ text-align: left; padding: 8px; border-bottom: 1px solid #eee; }}
      input {{ width: 100%; box-sizing: border-box; }}
      .actions {{ display: flex; gap: 8px; align-items: center; }}
      .domain-holds {{ border: 2px solid #b45309; background: #fffbeb; }}
    </style>
  </head>
  <body>
    <h1>Manage Newsletter</h1>
    <p class="small">{escape(message)}</p>

    <div class="card section domain-holds">
      <h2>Domain delivery holds</h2>
      <p><strong>{held_total} messages held for retry; {queued_blocked} remaining replay recipients currently held by domain.</strong></p>
      <p>Other domains continue receiving mail. Remove a hold after the provider clears the block;
         queued messages will retry automatically. New provider spam or policy rejections restore the hold.</p>
      <p>Testing sends only one queued message and keeps the domain held. Refresh for the receiving server's result;
         acceptance clears that message, and a nonexistent or disabled mailbox is unsubscribed.</p>
      <table><thead><tr><th>Domain</th><th>Held messages</th><th>Reason</th><th>Held since</th><th>Last test</th><th>Action</th></tr></thead>
        <tbody>{domain_html}</tbody></table>
      <form method="post" class="actions">
        <input type="hidden" name="domain_csrf" value="{domain_csrf}">
        <input name="domain" aria-label="Email domain" placeholder="hotmail.com" required>
        <button type="submit" name="action" value="block_domain">Hold domain</button>
      </form>
    </div>

    <div class="card section">
      <h3>Status</h3>
      <table>
        <tbody>
          <tr><th>Mode</th><td>{mode}</td></tr>
          <tr><th>Last Message Received</th><td>{last_message_received_html}</td></tr>
          <tr><th>Last Handled Message</th><td>{last_handled_message_received_html}</td></tr>
          <tr><th>Last Message Type</th><td>{last_handled_message_type}</td></tr>
          <tr><th>Last Delivery Progress</th><td>{last_delivery_progress}</td></tr>
        </tbody>
      </table>
    </div>

    <div class="card section">
      <h3>Bulk Update</h3>
      <p class="small">Paste emails.</p>
      <form method="post">
        <textarea name="bulk_input"></textarea>
        <div class="actions">
          <button type="submit" name="action" value="bulk_subscribe">Add Subscribers</button>
          <button type="submit" name="action" value="bulk_unsubscribe">Unsubscribe Users</button>
        </div>
      </form>
    </div>

    <div class="card section">
      <form method="post">
        <div class="actions" style="justify-content: space-between;">
          <h3 style="margin: 0;">Existing Entries</h3>
          <input type="hidden" name="action" value="save_existing" />
          <button type="submit">Save</button>
        </div>
        <table>
          <thead>
            <tr>
              <th>Email</th>
              <th>Name</th>
              <th>Rank</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {table_html}
          </tbody>
        </table>
      </form>
    </div>
    <script>
      function ordinalDay(day) {{
        const mod100 = day % 100;
        if (mod100 >= 11 && mod100 <= 13) {{
          return `${{day}}th`;
        }}
        const mod10 = day % 10;
        if (mod10 === 1) {{
          return `${{day}}st`;
        }}
        if (mod10 === 2) {{
          return `${{day}}nd`;
        }}
        if (mod10 === 3) {{
          return `${{day}}rd`;
        }}
        return `${{day}}th`;
      }}

      function formatLocalTimestamp(isoValue) {{
        const date = new Date(isoValue);
        if (Number.isNaN(date.getTime())) {{
          return isoValue;
        }}
        const parts = new Intl.DateTimeFormat(undefined, {{
          month: "long",
          day: "numeric",
          hour: "numeric",
          minute: "2-digit",
          hour12: true,
          timeZoneName: "short",
        }}).formatToParts(date);
        const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
        return `${{values.month}} ${{ordinalDay(Number(values.day))}}, ${{values.hour}}:${{values.minute}} ${{values.dayPeriod}} ${{values.timeZoneName}}`;
      }}

      for (const element of document.querySelectorAll(".js-local-ts")) {{
        const isoValue = element.dataset.iso;
        if (isoValue) {{
          element.textContent = formatLocalTimestamp(isoValue);
        }}
      }}
    </script>
  </body>
</html>
"""

    return html


app.add_url_rule(app_config.web.unsubscribe_path, view_func=unsub, methods=["GET", "POST"])
app.add_url_rule(app_config.web.manage_path, view_func=manage, methods=["GET", "POST"])


if __name__ == "__main__":
    app.run(
        host=app_config.web.bind,
        port=app_config.web.port,
        ssl_context=(app_config.web.resolved_tls_cert, app_config.web.resolved_tls_key),
    )
