#!/usr/bin/env python3

from flask import Flask, request, abort
import sqlite3
import hmac
import hashlib
import json
import base64
import secrets
import re
from datetime import datetime, timezone
from pathlib import Path
import bcrypt


def _load_json_from_config(filename: str) -> dict:
    base = Path(__file__).resolve().parent
    candidates = [
        base / ".." / "config" / filename,
        base / "config" / filename,
    ]
    for path in candidates:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
    searched = ", ".join(str(p) for p in candidates)
    raise FileNotFoundError(f"Missing {filename}. Searched: {searched}")


def _resolve_path(raw_path: str, domain: str | None = None) -> str:
    base = Path(__file__).resolve().parent
    config_dir = (base / ".." / "config").resolve()
    resolved = raw_path.replace("${config}", str(config_dir))
    if domain:
        resolved = resolved.replace("${domain}", domain)
    return resolved


web_cfg = _load_json_from_config("web.json")
db_cfg = _load_json_from_config("db.json")

DOMAIN = web_cfg["domain"]
DB_PATH = _resolve_path(db_cfg["db_path"], DOMAIN)
TOKEN_SECRET = web_cfg["token_secret"].encode("utf-8")
WEB_BIND = web_cfg["bind"]
WEB_PORT = int(web_cfg["port"])
TLS_CERT = _resolve_path(web_cfg["tls_cert"], DOMAIN)
TLS_KEY = _resolve_path(web_cfg["tls_key"], DOMAIN)
ADMIN_USER = web_cfg["admin_user"]
ADMIN_PASS_BCRYPT = web_cfg["admin_pass_bcrypt"]

app = Flask(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_auth() -> bool:
    if not ADMIN_USER or not ADMIN_PASS_BCRYPT:
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
    if user != ADMIN_USER:
        return False
    return bcrypt.checkpw(password.encode("utf-8"), ADMIN_PASS_BCRYPT.encode("utf-8"))


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
    return sqlite3.connect(DB_PATH)


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
    return hmac.new(TOKEN_SECRET, msg, hashlib.sha256).hexdigest()


@app.get("/unsub")
def unsub():
    e = (request.args.get("e") or "").lower()
    t = request.args.get("t") or ""
    s = request.args.get("s") or ""
    if not e or not t or not s:
        abort(400)

    if not hmac.compare_digest(sign(e, t), s):
        abort(403)

    con = _get_conn()
    cur = con.cursor()
    row = cur.execute("SELECT token FROM recipients WHERE email=?", (e,)).fetchone()
    if not row or row[0] != t:
        abort(403)

    now = _now_iso()
    cur.execute(
        "UPDATE recipients SET unsubscribed=1, unsubscribed_at=?, updated_at=? WHERE email=?",
        (now, now, e),
    )
    con.commit()
    return "Unsubscribed. You will no longer receive these emails.\n", 200


@app.route("/manage", methods=["GET", "POST"])
def manage():
    if not _require_auth():
        return _auth_challenge()

    message = ""
    if request.method == "POST":
        action = request.form.get("action", "")
        con = _get_conn()
        cur = con.cursor()

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
    rows = cur.execute(
        "SELECT id, email, rank, unsubscribed, name FROM recipients ORDER BY email ASC"
    ).fetchall()
    con.close()

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
    </style>
  </head>
  <body>
    <h1>Manage Newsletter</h1>
    <p class="small">{message}</p>

    <div class="card section">
      <h3>Bulk Update</h3>
      <p class="small">Paste CSV/newline/tab/colon/semicolon separated emails.</p>
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
  </body>
</html>
"""

    return html


if __name__ == "__main__":
    app.run(host=WEB_BIND, port=WEB_PORT, ssl_context=(TLS_CERT, TLS_KEY))
