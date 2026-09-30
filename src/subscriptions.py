"""Public newsletter preferences and a separate confirmation-email outbox."""

from contextlib import contextmanager
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
import hashlib
import hmac
import re
import secrets
import smtplib
import sqlite3
import ssl
import time
from urllib.parse import urlencode

from flask import Blueprint, abort, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix


POLICY_VERSION = "2026-09-30"
CONSENT_TEXT = "I want SPJ newsletter emails about its work, events, and fundraising."
DAY = 86400
SCHEMA = """
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
"""


@contextmanager
def database(config):
    con = sqlite3.connect(config.resolved_db_path, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        with con:
            yield con
    finally:
        con.close()


def initialize_database(config):
    with database(config) as con:
        con.executescript(SCHEMA)


def normalize_email(value):
    email = value.strip().lower()
    if len(email) > 254 or not email.isascii() or email.count("@") != 1:
        return None
    local, domain = email.split("@")
    if (not local or len(local) > 64 or local.startswith(".") or local.endswith(".")
            or ".." in local or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)):
        return None
    labels = domain.split(".")
    if len(labels) < 2 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in labels
    ):
        return None
    return email


def _cleanup(con, now):
    # Consent records are separate; short-lived request/rate-limit data expires.
    con.execute("DELETE FROM subscription_requests WHERE created_at < ?", (now - 7 * DAY,))


def _signature(config, row):
    data = f"subscription-v1\n{row['id']}\n{row['email']}\n{row['action']}\n{row['expires_at']}"
    return hmac.new(config.web.token_secret.encode(), data.encode(), hashlib.sha256).hexdigest()


def confirmation_token(config, row):
    return f"{row['id']}.{_signature(config, row)}"


def _lookup_token(con, config, token):
    if not re.fullmatch(r"[a-f0-9]{32}\.[a-f0-9]{64}", token):
        return None
    request_id, signature = token.split(".")
    row = con.execute("SELECT * FROM subscription_requests WHERE id=?", (request_id,)).fetchone()
    if (row is None or row["expires_at"] is None or row["expires_at"] < int(time.time())
            or not hmac.compare_digest(_signature(config, row), signature)):
        return None
    return row


def register_public_routes(app, config):
    initialize_database(config)
    app.config.update(
        SECRET_KEY=hmac.new(config.web.token_secret.encode(), b"web-session-v1", hashlib.sha256).digest(),
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        MAX_CONTENT_LENGTH=1024 * 1024,
    )
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    public = Blueprint("public", __name__, template_folder="templates", static_folder="static",
                       static_url_path="/newsletter-assets")

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    def check_csrf():
        supplied = request.form.get("csrf", "")
        expected = session.get("csrf", "")
        if not expected or not hmac.compare_digest(expected, supplied):
            abort(400, description="Please reload the page and try again.")

    @public.context_processor
    def context():
        return dict(csrf_token=csrf_token, consent_text=CONSENT_TEXT,
                    mail_enabled=config.web.confirmation_email_enabled)

    @public.after_request
    def protect_response(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; img-src 'self'; script-src 'none'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        if request.endpoint == "public.confirm":
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    @public.route("/", methods=["GET", "POST"])
    def home():
        mode = request.values.get("mode", "subscribe")
        if mode not in {"subscribe", "unsubscribe"}:
            abort(400)
        if request.method == "GET":
            return render_template("newsletter.html", mode=mode)
        check_csrf()
        if request.form.get("company", ""):
            return redirect(url_for("public.request_saved"), code=303)
        email = normalize_email(request.form.get("email", ""))
        if email is None:
            return render_template("newsletter.html", mode=mode,
                                   error="Enter a valid email address."), 400
        if mode == "subscribe" and request.form.get("consent") != "yes":
            return render_template("newsletter.html", mode=mode,
                                   error="Please confirm that you want to receive the newsletter."), 400
        now = int(time.time())
        ip_hash = hmac.new(config.web.token_secret.encode(),
                           ("request-ip:" + (request.remote_addr or "unknown")).encode(),
                           hashlib.sha256).hexdigest()
        with database(config) as con:
            con.execute("BEGIN IMMEDIATE")
            _cleanup(con, now)
            ip_count = con.execute(
                "SELECT COUNT(*) FROM subscription_requests WHERE ip_hash=? AND created_at>?",
                (ip_hash, now - 3600),
            ).fetchone()[0]
            if ip_count >= 10:
                return render_template("newsletter.html", mode=mode,
                                       error="Too many requests. Please try again in an hour."), 429
            # Use the same public response for missing addresses and duplicates.
            recent = con.execute(
                "SELECT 1 FROM subscription_requests WHERE email=? AND created_at>?",
                (email, now - 3600),
            ).fetchone()
            daily = con.execute(
                "SELECT COUNT(*) FROM subscription_requests WHERE created_at>?", (now - DAY,)
            ).fetchone()[0]
            exists = con.execute("SELECT 1 FROM recipients WHERE email=?", (email,)).fetchone()
            if not recent and daily < 100 and (mode == "subscribe" or exists):
                con.execute(
                    "INSERT INTO subscription_requests "
                    "(id,email,action,ip_hash,created_at,policy_version) VALUES (?,?,?,?,?,?)",
                    (secrets.token_hex(16), email, mode, ip_hash, now, POLICY_VERSION),
                )
        return redirect(url_for("public.request_saved"), code=303)

    @public.get("/request-saved")
    def request_saved():
        return render_template("subscription-result.html", title="Check your inbox",
                               result="requested")

    @public.get("/subscribe")
    def signup_link():
        return redirect(url_for("public.home"))

    @public.get("/unsubscribe")
    def unsubscribe_link():
        return redirect(url_for("public.home", mode="unsubscribe"))

    @public.get("/privacy")
    def privacy():
        return render_template("privacy.html")

    @public.route("/confirm", methods=["GET", "POST"])
    def confirm():
        token = request.values.get("t", "")
        if request.method == "POST":
            check_csrf()
        with database(config) as con:
            if request.method == "POST":
                con.execute("BEGIN IMMEDIATE")
            row = _lookup_token(con, config, token)
            if row is None:
                return render_template("subscription-result.html", title="This link has expired",
                                       result="invalid"), 400
            if row["confirmed_at"] is not None:
                return render_template("subscription-result.html", title="Already confirmed",
                                       result="already")
            if request.method == "GET":
                return render_template("subscription-confirm.html", action=row["action"], token=token)
            now = int(time.time())
            timestamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
            email = row["email"]
            if row["action"] == "subscribe":
                con.execute(
                    "INSERT INTO recipients (email,rank,unsubscribed,token,created_at,updated_at) "
                    "VALUES (?,100,0,?,?,?) ON CONFLICT(email) DO UPDATE SET "
                    "unsubscribed=0,unsubscribed_at=NULL,updated_at=excluded.updated_at",
                    (email, secrets.token_hex(16), timestamp, timestamp),
                )
            else:
                con.execute(
                    "UPDATE recipients SET unsubscribed=1,unsubscribed_at=?,updated_at=? WHERE email=?",
                    (timestamp, timestamp, email),
                )
            con.execute("UPDATE subscription_requests SET confirmed_at=? WHERE id=?", (now, row["id"]))
            con.execute(
                "DELETE FROM subscription_requests WHERE email=? AND confirmed_at IS NULL AND id<>?",
                (email, row["id"]),
            )
            con.execute(
                "INSERT INTO subscription_events "
                "(email,action,requested_at,confirmed_at,policy_version,consent_text) VALUES (?,?,?,?,?,?)",
                (email, row["action"], row["created_at"], now, row["policy_version"],
                 CONSENT_TEXT if row["action"] == "subscribe" else "Requested newsletter unsubscribe."),
            )
        return render_template("subscription-result.html", title="Your preference is saved",
                               result=row["action"])

    app.register_blueprint(public)


def send_next_confirmation(config):
    """Submit at most one confirmation; never reads newsletters or pending_replay."""
    if not config.web.confirmation_email_enabled or config.test.enabled:
        return False
    now = int(time.time())
    with database(config) as con:
        con.execute("BEGIN IMMEDIATE")
        _cleanup(con, now)
        row = con.execute(
            "SELECT * FROM subscription_requests WHERE sent_at IS NULL AND confirmed_at IS NULL "
            "AND (claimed_at IS NULL OR claimed_at<?) "
            "AND (last_attempt_at IS NULL OR last_attempt_at<?) ORDER BY created_at LIMIT 1",
            (now - 300, now - 600),
        ).fetchone()
        if row is None:
            return False
        expires = row["expires_at"] if row["expires_at"] and row["expires_at"] > now else now + DAY
        con.execute(
            "UPDATE subscription_requests SET claimed_at=?,last_attempt_at=?,expires_at=?,attempts=attempts+1 WHERE id=?",
            (now, now, expires, row["id"]),
        )
        row = con.execute("SELECT * FROM subscription_requests WHERE id=?", (row["id"],)).fetchone()
    token = confirmation_token(config, row)
    link = config.web.public_base_url.rstrip("/") + "/confirm?" + urlencode({"t": token})
    message = EmailMessage()
    message["From"] = config.smtp.from_header
    message["To"] = row["email"]
    message["Date"] = formatdate(localtime=False, usegmt=True)
    message["Message-ID"] = make_msgid(domain=config.web.domain)
    action = "subscription" if row["action"] == "subscribe" else "unsubscribe request"
    message["Subject"] = f"Confirm your SPJ newsletter {action}"
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(
        f"Please confirm your SPJ newsletter {action}:\n\n{link}\n\n"
        "Open the link and select Confirm. It expires in 24 hours.\n"
        "If you did not request this, ignore this email. Your preference will not change.\n\n"
        f"Privacy: {config.web.public_base_url.rstrip('/')}/privacy\n"
    )
    smtp = None
    accepted = False
    try:
        smtp = smtplib.SMTP(config.smtp.host, config.smtp.port, timeout=30)
        smtp.ehlo()
        if config.smtp.port != 25:
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        if config.smtp.password:
            smtp.login(config.smtp.username, config.smtp.password)
        smtp.send_message(message, from_addr=config.smtp.username, to_addrs=[row["email"]])
        accepted = True
    except Exception as error:
        print(f"Confirmation submission failed ({type(error).__name__}); retrying later", flush=True)
    finally:
        with database(config) as con:
            con.execute("UPDATE subscription_requests SET claimed_at=NULL,sent_at=? WHERE id=?",
                        (int(time.time()) if accepted else None, row["id"]))
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                smtp.close()
    return accepted
