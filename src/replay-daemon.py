#!/usr/bin/env python3

import imaplib
import ssl
import smtplib
import time
import random
import json
from email import policy, encoders
from email.parser import BytesParser
from email.utils import getaddresses, make_msgid
from html import escape
from html.parser import HTMLParser
from io import BytesIO
import sqlite3
import hmac
import hashlib
import re
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone
from pathlib import Path
from config import load_config
from delivery_state import ensure_schema, is_blocked, block_domain, hold_delivery, domain_for, policy_failure


app_config = load_config()

TEST_TAG = "+test"
MAX_MESSAGE_AGE = timedelta(minutes=15)

REPLY_TO_MODE = "original"  # "original" or "list"
INLINE_IMAGE_WIDTH = 600
_last_bounce_scan = None


def _remember_newsletter(message_id: str, uid: str = "", uidvalidity: str = "") -> None:
    if app_config.test.enabled or not message_id:
        return
    with sqlite3.connect(app_config.resolved_db_path) as con:
        ensure_schema(con)
        con.execute("INSERT INTO newsletter_messages(message_id,created_at,uid,uidvalidity) "
                    "VALUES (?,?,?,?) ON CONFLICT(message_id) DO UPDATE SET "
                    "uid=COALESCE(NULLIF(excluded.uid,''),newsletter_messages.uid), "
                    "uidvalidity=COALESCE(NULLIF(excluded.uidvalidity,''),newsletter_messages.uidvalidity)",
                    (message_id, datetime.now(timezone.utc).isoformat(), uid, uidvalidity))


def _permanent_mailbox_failure(dsn: str, diagnostic: str) -> bool:
    # RFC 3463: unknown mailbox or permanently disabled mailbox. Never classify
    # all 5xx responses as dead recipients: policy/IP blocks also use 5xx.
    if policy_failure(dsn, diagnostic) or re.search(r"\b(access denied|rate limit)\b",
                                                  diagnostic, re.IGNORECASE):
        return False
    if dsn in {"5.1.1", "5.2.1"}:
        return True
    # Yahoo/AT&T sometimes reports a disabled mailbox using generic 5.0.0.
    return dsn == "5.0.0" and bool(re.search(
        r"\bThis mailbox is disabled \(554\.30\)", diagnostic, re.IGNORECASE
    ))


def _scan_postfix_bounces() -> int:
    """Read trusted local Postfix records, never unverified inbound email."""
    log_dir = getattr(app_config.relay, "postfix_log_dir", "")
    if app_config.test.enabled or not log_dir:
        return 0
    with sqlite3.connect(app_config.resolved_db_path) as con:
        ensure_schema(con)
        policy_since = _get_config_value(con.cursor(), "domain_policy_started_at")
        if not policy_since:
            policy_since = datetime.now(timezone.utc).isoformat()
            _set_config_value(con.cursor(), "domain_policy_started_at", policy_since)
        sources = {row[0]: dict(message_id=row[0], uid=row[1], uidvalidity=row[2])
                   for row in con.execute("SELECT message_id,uid,uidvalidity FROM newsletter_messages")}
        pending = _get_config_value(con.cursor(), "pending_replay")
        pending_request = json.loads(pending) if pending else None
        if pending:
            sources[pending_request["message_id"]] = pending_request
        tests = {}
        for test in con.execute("SELECT id,test_message_id,message_id,recipient_id,uid,uidvalidity "
                                "FROM domain_tests WHERE test_message_id IS NOT NULL"):
            tests[test[1]] = test[0]
            sources[test[1]] = dict(message_id=test[2], uid=test[4], uidvalidity=test[5])
        if not sources:
            return 0
        # Reread the current and previous logs so a restart, rotation, or delayed
        # delivery cannot lose queue correlation. Updates are idempotent.
        queues = {}
        changed = 0
        blocked = 0
        for name in ("mail.log.1", "mail.log"):
            path = Path(log_dir) / name
            if not path.exists():
                if name == "mail.log":
                    raise FileNotFoundError("Configured Postfix mail.log is unavailable")
                continue
            with path.open(encoding="utf-8", errors="replace") as log:
                for line in log:
                    match = re.match(
                        r"^(\S+) \S+ postfix/(cleanup|qmgr|smtp)\[\d+\]: "
                        r"([A-Za-z0-9]+): (.*)$", line
                    )
                    if not match:
                        continue
                    timestamp, service, queue_id, detail = match.groups()
                    try:
                        bounced_at = datetime.fromisoformat(timestamp)
                        if bounced_at.tzinfo is None:
                            continue
                    except ValueError:
                        continue
                    if service == "cleanup":
                        identity = re.search(r"\bmessage-id=(<[^>]+>)", detail)
                        if identity:
                            queues[queue_id] = {
                                "newsletter": identity[1] in sources,
                                "message_id": identity[1],
                                "sender": False,
                            }
                    elif service == "qmgr":
                        if detail == "removed":
                            queues.pop(queue_id, None)
                        sender = re.search(r"\bfrom=<([^>]*)>", detail)
                        if sender and queue_id in queues:
                            queues[queue_id]["sender"] = (
                                sender[1].lower() == app_config.smtp.username.lower()
                            )
                    elif service == "smtp":
                        queue = queues.get(queue_id, {})
                        if not queue.get("newsletter") or not queue.get("sender"):
                            continue
                        delivery = re.match(
                            r"to=<([^>]+)>, .*\bdsn=(\d+\.\d+\.\d+), "
                            r"status=(sent|bounced|deferred) \((.*)\)$", detail
                        )
                        if not delivery:
                            continue
                        address, dsn, status, diagnostic = delivery.groups()
                        row = con.execute(
                            "SELECT id, updated_at FROM recipients "
                            "WHERE lower(email)=? AND unsubscribed=0", (address.lower(),)
                        ).fetchone()
                        if not row:
                            continue
                        source = sources[queue["message_id"]]
                        test_id = tests.get(queue["message_id"])
                        event_at = bounced_at.astimezone(timezone.utc).isoformat()
                        if status == "sent":
                            con.execute("DELETE FROM held_deliveries WHERE message_id=? "
                                        "AND recipient_id=? AND created_at<=?",
                                        (source["message_id"], row[0], event_at))
                            if test_id:
                                con.execute("UPDATE domain_tests SET status='sent',result=? WHERE id=?",
                                            ("Receiving server accepted the test. One queued message cleared; domain remains held.", test_id))
                            continue
                        if test_id:
                            result = (f"Test deferred (SMTP {dsn}); Postfix will retry it. Domain remains held."
                                      if status == "deferred" else
                                      f"Test bounced (SMTP {dsn}); message remains held. Domain remains held.")
                            if _permanent_mailbox_failure(dsn, diagnostic):
                                result = f"Mailbox does not exist or is disabled (SMTP {dsn}); checking recipient status."
                            con.execute("UPDATE domain_tests SET status=?,result=? WHERE id=?",
                                        ("submitted" if status == "deferred" else "bounced", result, test_id))
                        if policy_failure(dsn, diagnostic):
                            if bounced_at < datetime.fromisoformat(policy_since):
                                continue
                            event = con.execute(
                                "INSERT OR IGNORE INTO provider_failure_events VALUES (?,?,?)",
                                (queue_id, event_at, address.lower()),
                            )
                            if not event.rowcount:
                                continue
                            block_domain(con, domain_for(address),
                                         f"SMTP {dsn}: provider spam, reputation, or policy rejection", event_at)
                            blocked += 1
                            still_pending = (pending_request and source["message_id"] == pending_request["message_id"]
                                             and row[0] in pending_request.get("recipient_ids", []))
                            # Deferred mail still belongs to Postfix, which will retry it.
                            if status == "bounced" and not still_pending:
                                if source.get("uid") and source.get("uidvalidity"):
                                    hold_delivery(con, source, row[0], event_at)
                                else:
                                    print("Provider failure needs operator review: source mailbox identity unavailable")
                            continue
                        if status != "bounced" or not _permanent_mailbox_failure(dsn, diagnostic):
                            continue
                        # An old bounce must not reverse a newer subscription or
                        # operator update when logs are scanned again.
                        updated = datetime.fromisoformat(row[1])
                        if updated.tzinfo is None:
                            updated = updated.replace(tzinfo=timezone.utc)
                        if updated >= bounced_at:
                            if test_id:
                                con.execute("UPDATE domain_tests SET result=? WHERE id=?",
                                            ("Mailbox failure reported, but a newer recipient update exists; review required.", test_id))
                            continue
                        now = datetime.now(timezone.utc).isoformat()
                        con.execute(
                            "UPDATE recipients SET unsubscribed=1, unsubscribed_at=?, "
                            "updated_at=? WHERE id=?", (now, now, row[0])
                        )
                        changed += 1
                        con.execute("DELETE FROM held_deliveries WHERE recipient_id=?", (row[0],))
                        if test_id:
                            con.execute("UPDATE domain_tests SET result=? WHERE id=?",
                                        (f"Mailbox does not exist or is disabled (SMTP {dsn}); recipient unsubscribed and queued messages removed.", test_id))
        if changed:
            print(f"Automatically unsubscribed {changed} permanent mailbox failures")
        if blocked:
            print(f"Recorded {blocked} provider policy failures; affected domains paused")
        return changed


def _check_bounces() -> None:
    global _last_bounce_scan
    now = time.monotonic()
    if _last_bounce_scan is not None and now - _last_bounce_scan < 60:
        return
    _last_bounce_scan = now
    try:
        _scan_postfix_bounces()
    except Exception as error:
        # Keep mail flowing; do not expose recipient addresses or raw log lines.
        print(f"Bounce scan failed ({type(error).__name__}); retrying in 60 seconds")


def _batch_pause(imap) -> None:
    remaining = random.uniform(*app_config.relay.between_batches_sleep_seconds)
    while remaining > 0:
        interval = min(30, remaining)
        time.sleep(interval)
        remaining -= interval
        _check_bounces()
        _process_domain_tests(imap)


def load_contacts() -> list[tuple[int, str, str]]:
    if app_config.test.enabled:
        return [(index, email, "Test") for index, email in enumerate(app_config.test.normalized_contacts, start=1)]

    con = sqlite3.connect(app_config.resolved_db_path)
    cur = con.cursor()
    rows = cur.execute(
        "SELECT rank, email, token FROM recipients WHERE unsubscribed=0 ORDER BY rank ASC, id ASC"
    ).fetchall()
    con.close()
    return [(int(rank), email.lower(), token) for rank, email, token in rows]


def _get_config_value(cur, key: str) -> str | None:
    row = cur.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _set_config_value(cur, key: str, value: str) -> None:
    cur.execute(
        "INSERT INTO config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _set_config_if_newer(cur, key: str, value: str) -> None:
    current = _get_config_value(cur, key)
    if current:
        try:
            if datetime.fromisoformat(current) >= datetime.fromisoformat(value):
                return
        except ValueError:
            pass
    _set_config_value(cur, key, value)


def _set_delivery_progress(sent_count: int, total_count: int) -> None:
    con = sqlite3.connect(app_config.resolved_db_path)
    cur = con.cursor()
    _set_config_value(cur, "last_delivery_sent_count", str(sent_count))
    _set_config_value(cur, "last_delivery_total_count", str(total_count))
    con.commit()
    con.close()


def _start_delivery_status(message_received_at: str, message_type: str, total_count: int) -> None:
    con = sqlite3.connect(app_config.resolved_db_path)
    cur = con.cursor()
    _set_config_if_newer(cur, "last_handled_message_received_at", message_received_at)
    _set_config_value(cur, "last_handled_message_type", message_type)
    _set_config_value(cur, "last_delivery_sent_count", "0")
    _set_config_value(cur, "last_delivery_total_count", str(total_count))
    con.commit()
    con.close()


def connect_imap():
    m = imaplib.IMAP4_SSL(app_config.imap.host, app_config.imap.port)
    m.login(app_config.imap.username, app_config.imap.password)
    m.select("INBOX")
    return m


def connect_smtp():
    s = smtplib.SMTP(app_config.smtp.host, app_config.smtp.port, timeout=30)
    s.ehlo()
    if app_config.smtp.port != 25:
        ctx = ssl.create_default_context()
        s.starttls(context=ctx)
        s.ehlo()
    if app_config.smtp.password:
        s.login(app_config.smtp.username, app_config.smtp.password)
    return s


def set_or_replace(hdrs, k, v):
    if k in hdrs:
        hdrs.replace_header(k, v)
    else:
        hdrs[k] = v


def _extract_header_recipients(msg) -> set[str]:
    recipients: set[str] = set()
    for hdr in ("To", "Cc", "Bcc", "Delivered-To", "X-Original-To", "X-Envelope-To", "Envelope-To"):
        raw = msg.get_all(hdr, [])
        if not raw:
            continue
        for _name, addr in getaddresses(raw):
            if addr:
                recipients.add(addr.lower())
    return recipients


def _has_test_tag(recipients: set[str]) -> bool:
    for addr in recipients:
        local = addr.split("@", 1)[0]
        if TEST_TAG in local:
            return True
    return False


def _extract_sender_email(msg) -> str | None:
    for hdr in ("From", "Reply-To"):
        raw = msg.get_all(hdr, [])
        if not raw:
            continue
        for _name, addr in getaddresses(raw):
            if addr:
                return addr.lower()
    return None


def _resize_inline_images(msg) -> None:
    try:
        from PIL import Image, ImageOps
    except Exception as e:
        print(f"Pillow dependency not found: {str(e)}")
        return

    for part in msg.walk():
        if part.get_content_maintype() != "image":
            continue

        payload = part.get_payload(decode=True)
        if not payload:
            continue
        filename = part.get_filename()

        try:
            with Image.open(BytesIO(payload)) as img:
                im = ImageOps.exif_transpose(img)
                original_width, original_height = im.size
                if original_width == 0 or original_height == 0:
                    continue
                if original_width <= INLINE_IMAGE_WIDTH:
                    continue
                new_width = INLINE_IMAGE_WIDTH
                new_height = int((original_height / original_width) * new_width)

                if im.mode not in ("RGB", "L"):
                    bg = Image.new("RGB", im.size, (255, 255, 255))
                    if im.mode == "RGBA":
                        bg.paste(im, mask=im.split()[3])
                    else:
                        bg.paste(im)
                    im = bg
                elif im.mode == "L":
                    im = im.convert("RGB")

                im = im.resize((new_width, new_height), Image.LANCZOS)  # type: ignore[attr-defined]
                out = BytesIO()
                im.save(out, format="JPEG", quality=100)
                jpeg_bytes = out.getvalue()
        except Exception as e:
            print("Failed to resize: " + str(e))
            continue

        part.set_payload(jpeg_bytes)
        part.set_type("image/jpeg")
        if "Content-Transfer-Encoding" in part:
            del part["Content-Transfer-Encoding"]
        encoders.encode_base64(part)

        if filename:
            if "." in filename:
                filename = filename.rsplit(".", 1)[0]
            filename = f"{filename}.jpg"
            part.set_param("name", filename, header="Content-Type")
            if "Content-Disposition" in part:
                part.set_param("filename", filename, header="Content-Disposition", replace=True)
            else:
                part.add_header("Content-Disposition", "inline", filename=filename)


def _sign_unsub(email_addr: str, token: str) -> str:
    msg = f"{email_addr}\n{token}".encode("utf-8")
    return hmac.new(app_config.web.token_secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _build_unsub_link(email_addr: str, token: str) -> str:
    qs = urlencode({"e": email_addr, "t": token, "s": _sign_unsub(email_addr, token)})
    return f"{app_config.web.public_base_url}{app_config.web.unsubscribe_path}?{qs}"


def _insert_html_before_close(html: str, snippet: str) -> str:
    for closing_tag in ("</body>", "</html>"):
        match = re.search(closing_tag, html, flags=re.IGNORECASE)
        if match:
            return f"{html[:match.start()]}{snippet}{html[match.start():]}"
    return f"{html}{snippet}"


def _normalize_inline_content_ids(msg) -> None:
    replacements: dict[str, str] = {}
    filenames: set[str] = set()
    for index, part in enumerate(msg.walk(), start=1):
        if "Content-ID" not in part:
            continue
        raw_value = str(part["Content-ID"]).strip()
        cid = raw_value[1:-1] if raw_value.startswith("<") and raw_value.endswith(">") else raw_value
        digest = hashlib.sha1(cid.encode("utf-8")).hexdigest()[:16]
        new_cid = f"relay-{index}-{digest}@inline"
        replacements[cid] = new_cid
        del part["Content-ID"]
        part._headers.append(("Content-ID", f"<{new_cid}>"))
        if part.get_content_maintype() == "image":
            filename = part.get_filename() or f"inline-{index}.{part.get_content_subtype()}"
            stem, dot, extension = filename.rpartition(".")
            suffix = 1
            original = filename
            while filename.casefold() in filenames:
                filename = f"{stem}-{suffix}.{extension}" if dot else f"{original}-{suffix}"
                suffix += 1
            filenames.add(filename.casefold())
            part.set_param("name", filename, header="Content-Type")
            if "Content-Disposition" in part:
                del part["Content-Disposition"]
            part.add_header("Content-Disposition", "inline", filename=filename)

    if not replacements:
        return

    for part in msg.walk():
        if part.get_content_maintype() != "text":
            continue
        subtype = part.get_content_subtype()
        text = part.get_content()
        updated = text
        for old_cid, new_cid in replacements.items():
            updated = updated.replace(f"cid:{old_cid}", f"cid:{new_cid}")
            updated = updated.replace(f"[cid:{old_cid}]", f"[cid:{new_cid}]")
        if updated != text:
            part.set_content(updated, subtype=subtype, charset=part.get_content_charset() or "utf-8")


class _ResponsiveImages(HTMLParser):
    """Replace image tags without reserializing the surrounding email HTML."""

    def __init__(self, html: str):
        super().__init__(convert_charrefs=False)
        self.html = html
        self.line_offsets = [0]
        self.line_offsets.extend(match.end() for match in re.finditer("\n", html))
        self.edits: list[tuple[int, int, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag != "img":
            return
        attributes = dict(attrs)
        # Keep the original display width as a ceiling, capped at our image size.
        width = attributes.get("width") or ""
        limit = INLINE_IMAGE_WIDTH
        if width.isascii() and width.isdigit() and int(width) > 0:
            limit = min(int(width), limit)
        styles = []
        for declaration in (attributes.get("style") or "").split(";"):
            name, separator, _value = declaration.partition(":")
            if separator and name.strip().lower() not in {
                "width", "height", "min-width", "min-height", "max-width", "max-height"
            }:
                styles.append(declaration.strip())
        styles.extend(["width:100%", f"max-width:{limit}px", "height:auto"])
        attributes.pop("height", None)
        attributes["width"] = str(limit)
        attributes["style"] = ";".join(styles)
        rendered = " ".join(
            key if value is None else f'{key}="{escape(value, quote=True)}"'
            for key, value in attributes.items()
        )
        raw = self.get_starttag_text()
        line, column = self.getpos()
        start = self.line_offsets[line - 1] + column
        self.edits.append((start, start + len(raw), f"<img {rendered}>"))

    def render(self) -> str:
        self.feed(self.html)
        self.close()
        html = self.html
        for start, end, replacement in reversed(self.edits):
            html = html[:start] + replacement + html[end:]
        return html


def _make_images_responsive(msg) -> None:
    for part in msg.walk():
        if part.get_content_type() != "text/html":
            continue
        html = part.get_content()
        updated = _ResponsiveImages(html).render()
        if updated != html:
            part.set_content(updated, subtype="html", charset=part.get_content_charset() or "utf-8")


def _append_unsub(msg, link: str):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() != "text":
                continue
            subtype = part.get_content_subtype()
            text = part.get_content()
            if subtype == "html":
                text = _insert_html_before_close(
                    text,
                    f"<br><br><p>Unsubscribe: <a href=\"{link}\">{link}</a></p>",
                )
            else:
                text = f"{text}\n\nUnsubscribe: {link}\n"
            part.set_content(text, subtype=subtype, charset=part.get_content_charset() or "utf-8")
    else:
        subtype = msg.get_content_subtype()
        text = msg.get_content()
        if subtype == "html":
            text = _insert_html_before_close(
                text,
                f"<br><br><p>Unsubscribe: <a href=\"{link}\">{link}</a></p>",
            )
        else:
            text = f"{text}\n\nUnsubscribe: {link}\n"
        msg.set_content(text, subtype=subtype, charset=msg.get_content_charset() or "utf-8")


def forward_full_fidelity(raw_bytes: bytes, rcpt: str, token: str):
    msg = BytesParser(policy=policy.SMTP).parsebytes(raw_bytes)

    # These authenticate the original message, which we are about to modify.
    # The outbound MTA signs the final per-recipient message after serialization.
    for header in (
        "DKIM-Signature", "DomainKey-Signature", "Authentication-Results",
        "ARC-Seal", "ARC-Message-Signature", "ARC-Authentication-Results",
    ):
        if header in msg:
            del msg[header]

    # Minimal header surgery (preserves MIME parts/attachments)
    _resize_inline_images(msg)
    # 1) Ensure single recipient in To:
    set_or_replace(msg, "To", rcpt)

    # 2) Your visible From (can also keep original if you prefer)
    set_or_replace(msg, "From", app_config.smtp.from_header)

    # 3) Reply-To behavior
    if REPLY_TO_MODE == "original":
        # if original From exists in message (it should), keep Reply-To to original sender
        # if you'd rather force replies elsewhere, set REPLY_TO_MODE="list"
        pass
    else:
        set_or_replace(msg, "Reply-To", app_config.smtp.from_header)

    # Optional: List headers (helps legit mailing list semantics)
    # Note: you said you already append an unsubscribe link; keep your existing mechanism here.
    # set_or_replace(msg, "List-ID", "Your List <list.yourdomain.com>")

    unsub_link = _build_unsub_link(rcpt, token)
    set_or_replace(msg, "List-Unsubscribe", f"<{unsub_link}>")
    _append_unsub(msg, unsub_link)
    _normalize_inline_content_ids(msg)
    _make_images_responsive(msg)
    data = msg.as_bytes(policy=policy.SMTP)
    return data


def forward_test_message(raw_bytes: bytes, sender: str) -> bytes:
    raw_bytes = forward_full_fidelity(raw_bytes, sender, "Test")
    msg = BytesParser(policy=policy.SMTP).parsebytes(raw_bytes)

    subj = msg.get("Subject")
    if subj:
        set_or_replace(msg, "Subject", f"[TEST] {subj}")
    else:
        set_or_replace(msg, "Subject", "[TEST]")

    data = msg.as_bytes(policy=policy.SMTP)
    return data


def _finish_replay_recipient(recipient_id: int, sent: bool) -> None:
    with sqlite3.connect(app_config.resolved_db_path) as con:
        cur = con.cursor()
        raw_request = _get_config_value(cur, "pending_replay")
        if raw_request is None:
            raise RuntimeError("Pending replay was removed during delivery")
        request = json.loads(raw_request)
        request["recipient_ids"].remove(recipient_id)
        counter = "sent_count" if sent else "skipped_count"
        request[counter] += 1
        _set_config_value(cur, "last_delivery_sent_count", str(request["sent_count"]))
        if request["recipient_ids"]:
            _set_config_value(cur, "pending_replay", json.dumps(request))
        else:
            cur.execute("DELETE FROM config WHERE key='pending_replay'")


def _hold_replay_recipient(recipient_id: int) -> None:
    with sqlite3.connect(app_config.resolved_db_path) as con:
        ensure_schema(con)
        cur = con.cursor()
        request = json.loads(_get_config_value(cur, "pending_replay"))
        hold_delivery(con, request, recipient_id)
        request["recipient_ids"].remove(recipient_id)
        request["held_count"] = request.get("held_count", 0) + 1
        if request["recipient_ids"]:
            _set_config_value(cur, "pending_replay", json.dumps(request))
        else:
            cur.execute("DELETE FROM config WHERE key='pending_replay'")


def _fetch_retry_source(imap, source: dict) -> bytes:
    validity = imap.response("UIDVALIDITY")[1]
    if not validity or validity[0].decode() != source["uidvalidity"]:
        raise RuntimeError("Retry mailbox identity changed")
    status, fetched = imap.uid("fetch", str(source["uid"]), "(BODY.PEEK[])")
    if status != "OK":
        raise RuntimeError("Retry source unavailable")
    raw = b"".join(item[1] for item in fetched if isinstance(item, tuple))
    msg = BytesParser(policy=policy.SMTP).parsebytes(raw)
    if str(msg.get("Message-ID")) != source["message_id"]:
        raise RuntimeError("Retry source identity changed")
    if app_config.imap.normalized_filter_recipient not in str(msg.get("From", "")).lower():
        raise RuntimeError("Retry source sender does not match")
    return raw


def _process_held_deliveries(imap) -> bool:
    if app_config.test.enabled:
        return False
    with sqlite3.connect(app_config.resolved_db_path) as con:
        ensure_schema(con)
        rows = con.execute("SELECT message_id,recipient_id,uid,uidvalidity FROM held_deliveries "
                           "ORDER BY created_at,recipient_id").fetchall()
    attempts = 0
    sources = {}
    for message_id, recipient_id, uid, uidvalidity in rows:
        _check_bounces()
        _process_domain_tests(imap)
        with sqlite3.connect(app_config.resolved_db_path) as con:
            row = con.execute("SELECT email,token FROM recipients WHERE id=? AND unsubscribed=0",
                              (recipient_id,)).fetchone()
            if not row:
                con.execute("DELETE FROM held_deliveries WHERE message_id=? AND recipient_id=?",
                            (message_id, recipient_id))
                continue
            if is_blocked(con, row[0]):
                continue
            if con.execute("SELECT 1 FROM domain_tests WHERE message_id=? AND recipient_id=? "
                           "AND status IN ('sending','submitted')", (message_id, recipient_id)).fetchone():
                continue
            # A later successful Postfix event may already have cleared this job.
            if not con.execute("SELECT 1 FROM held_deliveries WHERE message_id=? AND recipient_id=?",
                               (message_id, recipient_id)).fetchone():
                continue
        source = dict(message_id=message_id, uid=uid, uidvalidity=uidvalidity)
        smtp = None
        try:
            if message_id not in sources:
                sources[message_id] = _fetch_retry_source(imap, source)
            mime_bytes = forward_full_fidelity(sources[message_id], *row)
            smtp = connect_smtp()
            smtp.sendmail(app_config.smtp.username, [row[0]], mime_bytes)
            with sqlite3.connect(app_config.resolved_db_path) as con:
                con.execute("DELETE FROM held_deliveries WHERE message_id=? AND recipient_id=?",
                            (message_id, recipient_id))
            print("Submitted a released domain retry")
        except Exception as error:
            print(f"Domain retry failed ({type(error).__name__}); retained for retry")
        finally:
            if smtp is not None:
                try:
                    smtp.quit()
                except Exception:
                    smtp.close()
        attempts += 1
        time.sleep(random.uniform(*app_config.relay.per_recipient_sleep_seconds))
        if attempts % app_config.relay.batch_size == 0:
            _batch_pause(imap)
    return attempts > 0


def _process_domain_tests(imap) -> bool:
    """Send one explicitly requested probe without releasing the domain."""
    if app_config.test.enabled:
        return False
    with sqlite3.connect(app_config.resolved_db_path) as con:
        ensure_schema(con)
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        con.execute("UPDATE domain_tests SET status='error',result=? "
                    "WHERE status='sending' AND created_at<?",
                    ("Test interrupted. Queued message retained; review delivery logs before testing again.", cutoff))
        test = con.execute("SELECT id,domain FROM domain_tests WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
        if not test:
            return False
        test_id, domain = test
        if not con.execute("SELECT 1 FROM domain_blocks WHERE domain=? AND released_at IS NULL", (domain,)).fetchone():
            con.execute("UPDATE domain_tests SET status='error',result=? WHERE id=?",
                        ("Domain hold was already removed; normal retries will handle queued messages.", test_id))
            return False
        candidate = con.execute(
            "SELECT h.message_id,h.recipient_id,h.uid,h.uidvalidity,r.email,r.token "
            "FROM held_deliveries h JOIN recipients r ON r.id=h.recipient_id "
            "WHERE r.unsubscribed=0 AND lower(substr(r.email,instr(r.email,'@')+1))=? "
            "ORDER BY h.created_at,h.recipient_id LIMIT 1", (domain,),
        ).fetchone()
        if not candidate:
            pending = _get_config_value(con.cursor(), "pending_replay")
            request = json.loads(pending) if pending else None
            contacts = con.execute("SELECT id,email,token FROM recipients WHERE unsubscribed=0 "
                                   "AND lower(substr(email,instr(email,'@')+1))=? ORDER BY id", (domain,)).fetchall()
            queued = set(request["recipient_ids"]) if request else set()
            contact = next((row for row in contacts if row[0] in queued), None)
            if contact:
                hold_delivery(con, request, contact[0])
                request["recipient_ids"].remove(contact[0])
                request["held_count"] = request.get("held_count", 0) + 1
                if request["recipient_ids"]:
                    _set_config_value(con.cursor(), "pending_replay", json.dumps(request))
                else:
                    con.execute("DELETE FROM config WHERE key='pending_replay'")
                candidate = (request["message_id"], contact[0], request["uid"], request["uidvalidity"], contact[1], contact[2])
        if not candidate:
            con.execute("UPDATE domain_tests SET status='error',result=? WHERE id=?",
                        ("No queued message for an active recipient in this domain.", test_id))
            return False
        message_id, recipient_id, uid, uidvalidity, address, token = candidate
        identity = make_msgid(domain=app_config.web.domain)
        con.execute("UPDATE domain_tests SET status='sending',result=?,message_id=?,recipient_id=?,"
                    "uid=?,uidvalidity=?,test_message_id=? WHERE id=? AND status='pending'",
                    ("Sending one queued message; domain remains held.", message_id, recipient_id,
                     uid, uidvalidity, identity, test_id))
    smtp = None
    try:
        raw = _fetch_retry_source(imap, dict(message_id=message_id, uid=uid, uidvalidity=uidvalidity))
        outgoing = BytesParser(policy=policy.SMTP).parsebytes(forward_full_fidelity(raw, address, token))
        set_or_replace(outgoing, "Message-ID", identity)
        smtp = connect_smtp()
        smtp.sendmail(app_config.smtp.username, [address], outgoing.as_bytes(policy=policy.SMTP))
        with sqlite3.connect(app_config.resolved_db_path) as con:
            con.execute("UPDATE domain_tests SET status='submitted',result=?,submitted_at=? WHERE id=?",
                        ("Submitted to Postfix; awaiting the receiving server's result. Domain remains held.",
                         datetime.now(timezone.utc).isoformat(), test_id))
        print("Submitted one requested domain test; awaiting provider result")
    except Exception as error:
        with sqlite3.connect(app_config.resolved_db_path) as con:
            con.execute("UPDATE domain_tests SET status='error',result=? WHERE id=?",
                        (f"Test submission failed ({type(error).__name__}); queued message retained.", test_id))
        print(f"Domain test failed ({type(error).__name__}); queued message retained")
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                smtp.close()
    time.sleep(random.uniform(*app_config.relay.per_recipient_sleep_seconds))
    return True


def _process_pending_replay(imap) -> bool:
    """Regenerate an explicitly staged recipient subset without rewinding IMAP."""
    with sqlite3.connect(app_config.resolved_db_path) as con:
        raw_request = _get_config_value(con.cursor(), "pending_replay")
    if not raw_request:
        return False
    if app_config.test.enabled:
        raise RuntimeError("Pending replay requires production mode")
    request = json.loads(raw_request)
    validity = imap.response("UIDVALIDITY")[1]
    if not validity or validity[0].decode() != request["uidvalidity"]:
        raise RuntimeError("Pending replay mailbox identity changed; operator review required")
    status, fetched = imap.uid("fetch", str(request["uid"]), "(BODY.PEEK[])")
    if status != "OK":
        raise RuntimeError("Pending replay source message could not be fetched")
    raw = b"".join(item[1] for item in fetched if isinstance(item, tuple))
    msg = BytesParser(policy=policy.SMTP).parsebytes(raw)
    if str(msg.get("Message-ID")) != request["message_id"]:
        raise RuntimeError("Pending replay source message identity changed")
    if app_config.imap.normalized_filter_recipient not in (msg.get("From") or "").lower():
        raise RuntimeError("Pending replay source does not match the sender filter")
    _remember_newsletter(request["message_id"], request["uid"], request["uidvalidity"])

    print(f"Processing pending replay for {len(request['recipient_ids'])} recipients")
    attempts = 0
    held = 0
    for recipient_id in request["recipient_ids"]:
        _check_bounces()
        _process_domain_tests(imap)
        # A requested test can move a recipient from this snapshot to the held queue.
        with sqlite3.connect(app_config.resolved_db_path) as con:
            pending = _get_config_value(con.cursor(), "pending_replay")
            if not pending or recipient_id not in json.loads(pending)["recipient_ids"]:
                continue
        # Recheck unsubscribe status immediately before each submission.
        with sqlite3.connect(app_config.resolved_db_path) as con:
            row = con.execute(
                "SELECT email, token FROM recipients WHERE id=? AND unsubscribed=0",
                (recipient_id,),
            ).fetchone()
            blocked = bool(row and is_blocked(con, row[0]))
        if row is None:
            _finish_replay_recipient(recipient_id, sent=False)
            continue
        if blocked:
            _hold_replay_recipient(recipient_id)
            held += 1
            continue
        rcpt, token = row
        smtp = None
        try:
            mime_bytes = forward_full_fidelity(raw, rcpt, token)
            smtp = connect_smtp()
            smtp.sendmail(app_config.smtp.username, [rcpt], mime_bytes)
        except Exception as error:
            # SMTP errors can contain recipient addresses; log only the type.
            print(f"Replay submission failed ({type(error).__name__}); retained for retry")
        else:
            # Save acceptance before closing SMTP, which can fail independently.
            _finish_replay_recipient(recipient_id, sent=True)
        finally:
            if smtp is not None:
                try:
                    smtp.quit()
                except Exception:
                    smtp.close()
        time.sleep(random.uniform(*app_config.relay.per_recipient_sleep_seconds))
        attempts += 1
        if attempts % app_config.relay.batch_size == 0:
            _process_held_deliveries(imap)
            _batch_pause(imap)
    if held:
        print(f"Held {held} recipients for blocked domains; other delivery continues")
    return True


def main_loop():
    imap = connect_imap()
    con = None
    try:
        _check_bounces()
        _process_domain_tests(imap)
        _process_held_deliveries(imap)
        if _process_pending_replay(imap):
            return
        con = sqlite3.connect(app_config.resolved_db_path)
        cur = con.cursor()
        last_uid_raw = _get_config_value(cur, "last_uid")
        con.close()
        con = None

        last_uid = int(last_uid_raw) if last_uid_raw else 0
        status, data = imap.uid("search", None, f"UID {last_uid + 1}:*")
        if status != "OK":
            return

        uids = data[0].split() if data and data[0] else []
        for uid in uids:
            uid_text = uid.decode() if hasattr(uid, "decode") else str(uid)
            st, fetched = imap.uid("fetch", uid, "(RFC822 INTERNALDATE)")
            if st != "OK" or not fetched or not fetched[0]:
                continue
            raw = fetched[0][1]
            internaldate = imaplib.Internaldate2tuple(fetched[0][0])
            if internaldate is None:
                continue
            msg_dt = datetime.fromtimestamp(time.mktime(internaldate), tz=timezone.utc)
            is_stale = datetime.now(timezone.utc) - msg_dt > MAX_MESSAGE_AGE

            print(f"check {msg_dt.isoformat()}")

            con = sqlite3.connect(app_config.resolved_db_path)
            cur = con.cursor()
            last_seen_raw = _get_config_value(cur, "last_processed_at")
            last_seen = datetime.fromisoformat(last_seen_raw) if last_seen_raw else None
            if last_seen and msg_dt <= last_seen:
                _set_config_if_newer(cur, "last_message_received_at", msg_dt.isoformat())
                _set_config_value(cur, "last_uid", uid_text)
                con.commit()
                con.close()
                con = None
                continue

            if is_stale:
                print(f"Skipping stale message from {msg_dt.isoformat()}")
                _set_config_if_newer(cur, "last_message_received_at", msg_dt.isoformat())
                _set_config_value(cur, "last_processed_at", msg_dt.isoformat())
                _set_config_value(cur, "last_uid", uid_text)
                con.commit()
                con.close()
                con = None
                try:
                    imap.uid("store", uid, "+FLAGS", "(\\Seen)")
                except Exception as e:
                    print("IMAGE mark as read exception: " + str(e))
                continue

            print(f"Received message at {msg_dt.isoformat()}")
            _set_config_if_newer(cur, "last_message_received_at", msg_dt.isoformat())
            _set_config_value(cur, "last_processed_at", msg_dt.isoformat())
            _set_config_value(cur, "last_uid", uid_text)
            con.commit()
            con.close()
            con = None

            # Mark as seen (optional)
            try:
                imap.uid("store", uid, "+FLAGS", "(\\Seen)")
            except Exception as e:
                print("IMAGE mark as read exception: " + str(e))

            msg = BytesParser(policy=policy.SMTP).parsebytes(raw)
            from_hdr = (msg.get("From") or "").lower()
            is_test = _has_test_tag(_extract_header_recipients(msg))
            filter_recipient = app_config.imap.normalized_filter_recipient
            if app_config.test.enabled and app_config.test.normalized_filter_recipient:
                filter_recipient = app_config.test.normalized_filter_recipient
            if filter_recipient not in from_hdr:
                continue

            print(f"Received message {uid_text}: {msg.get('subject')}")
            contacts = load_contacts()

            if is_test:
                sender = _extract_sender_email(msg)
                _start_delivery_status(msg_dt.isoformat(), "Test", 1 if sender else 0)
                if sender:
                    print(f"Test recipient detected; relaying only to sender {sender}")
                    mime_bytes = forward_test_message(raw, sender)
                    smtp = connect_smtp()
                    try:
                        smtp.sendmail(app_config.smtp.username, [sender], mime_bytes)
                        _set_delivery_progress(1, 1)
                    except Exception as e:
                        print(f"Failed to send test mail: {str(e)}")
                    finally:
                        smtp.quit()
                else:
                    print("Test recipient detected but no sender address found; skipping")
                time.sleep(random.uniform(*app_config.relay.per_message_sleep_seconds))
                continue

            # Send in priority order
            completed = 0
            attempts = 0
            source = dict(message_id=str(msg.get("Message-ID") or ""), uid=uid_text,
                          uidvalidity=imap.response("UIDVALIDITY")[1][0].decode())
            _remember_newsletter(source["message_id"], source["uid"], source["uidvalidity"])
            _start_delivery_status(msg_dt.isoformat(), "Newsletter", len(contacts))
            for _rank, rcpt, token in contacts:
                _check_bounces()
                _process_domain_tests(imap)
                with sqlite3.connect(app_config.resolved_db_path) as active_con:
                    if not app_config.test.enabled and not active_con.execute(
                        "SELECT 1 FROM recipients WHERE lower(email)=? AND unsubscribed=0",
                        (rcpt.lower(),),
                    ).fetchone():
                        continue
                    if not app_config.test.enabled and is_blocked(active_con, rcpt):
                        recipient_id = active_con.execute("SELECT id FROM recipients WHERE lower(email)=?",
                                                          (rcpt.lower(),)).fetchone()[0]
                        hold_delivery(active_con, source, recipient_id)
                        continue

                try:
                    mime_bytes = forward_full_fidelity(raw, rcpt, token)

                    # Envelope sender can differ from header From:
                    smtp = connect_smtp()
                    try:
                        smtp.sendmail(app_config.smtp.username, [rcpt], mime_bytes)
                    except Exception as e:
                        print(f"Failed to send mail: {str(e)}")
                    finally:
                        smtp.quit()

                    time.sleep(random.uniform(*app_config.relay.per_recipient_sleep_seconds))
                except Exception as e:
                    print(f"Exception sending to {rcpt}: " + str(e))
                completed += 1
                _set_delivery_progress(completed, len(contacts))
                attempts += 1
                if attempts % app_config.relay.batch_size == 0:
                    _batch_pause(imap)

            time.sleep(random.uniform(*app_config.relay.per_message_sleep_seconds))
    except Exception as e:
        print("Exception: " + str(e))
    finally:
        try:
            imap.logout()
        except Exception:
            pass
        try:
            con.close()
        except Exception:
            pass


if __name__ == "__main__":
    while True:
        try:
            main_loop()
        except Exception:
            pass
        time.sleep(app_config.relay.poll_seconds)
