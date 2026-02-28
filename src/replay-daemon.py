#!/usr/bin/env python3

import imaplib, ssl, smtplib, time, random
import json
from pathlib import Path
from email import policy, encoders
from email.parser import BytesParser
from email.utils import getaddresses
from io import BytesIO
import sqlite3
import hmac
import hashlib
from urllib.parse import urlencode
from datetime import datetime, timezone

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


def _resolve_db_path(raw_path: str) -> str:
    base = Path(__file__).resolve().parent
    config_dir = (base / ".." / "config").resolve()
    return raw_path.replace("${config}", str(config_dir))


imap_cfg = _load_json_from_config("imap.json")
smtp_cfg = _load_json_from_config("smtp.json")
db_cfg = _load_json_from_config("db.json")
relay_cfg = _load_json_from_config("relay.json")
web_cfg = _load_json_from_config("web.json")

IMAP_HOST = imap_cfg["host"]
IMAP_PORT = int(imap_cfg["port"])
IMAP_USER = imap_cfg["username"]
IMAP_PASS = imap_cfg["password"]

SMTP_HOST = smtp_cfg["host"]
SMTP_PORT = int(smtp_cfg["port"])
SMTP_USER = smtp_cfg["username"]
SMTP_PASS = smtp_cfg["password"]
FROM_HEADER = smtp_cfg["from"]

DB_PATH = _resolve_db_path(db_cfg["db_path"])
POLL_SECONDS = int(relay_cfg["poll_seconds"])
PUBLIC_BASE_URL = relay_cfg["public_base_url"]
UNSUBSCRIBE_PATH = relay_cfg["unsubscribe_path"]
TOKEN_SECRET = web_cfg["token_secret"].encode("utf-8")

_filter_recipient = imap_cfg["filter_recipient"]
if isinstance(_filter_recipient, str):
    ALLOWED_FROMS = [_filter_recipient]
else:
    ALLOWED_FROMS = list(_filter_recipient)
ALLOWED_FROMS = [v.lower() for v in ALLOWED_FROMS]
TEST_TAG = "+test"

# throttling
PER_RCPT_SLEEP_RANGE = (1.0, 3.0)   # jitter between recipients
PER_MESSAGE_SLEEP_RANGE = (5.0, 12.0)

REPLY_TO_MODE = "original"  # "original" or "list"
INLINE_IMAGE_WIDTH = 600


def load_contacts() -> list[tuple[int, str, str]]:
    con = sqlite3.connect(DB_PATH)
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


def connect_imap():
    m = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    m.login(IMAP_USER, IMAP_PASS)
    m.select("INBOX")
    return m


def connect_smtp():
    ctx = ssl.create_default_context()
    s = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
    s.ehlo()
    s.starttls(context=ctx)
    s.ehlo()
    s.login(SMTP_USER, SMTP_PASS)
    return s


def set_or_replace(hdrs, k, v):
    if k in hdrs:
        hdrs.replace_header(k, v)
    else:
        hdrs[k] = v


def _is_bounce(msg) -> bool:
    if msg.get_content_type() == "multipart/report":
        report_type = msg.get_param("report-type") or ""
        if report_type.lower() == "delivery-status":
            return True
    for part in msg.walk():
        if part.get_content_type() == "message/delivery-status":
            return True
    subj = (msg.get("Subject") or "").lower()
    return "undelivered" in subj or "delivery status notification" in subj


def _extract_bounce_recipients(msg) -> set[str]:
    recipients: set[str] = set()
    for part in msg.walk():
        if part.get_content_type() != "message/delivery-status":
            continue
        payload = part.get_payload()
        if not isinstance(payload, list):
            continue
        for block in payload:
            for key in ("Final-Recipient", "Original-Recipient"):
                val = block.get(key)
                if not val:
                    continue
                if ";" in val:
                    val = val.split(";", 1)[1].strip()
                recipients.add(val.lower())
    return recipients


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
        print("Pillow dependency not found")
        return

    for part in msg.walk():
        if part.get_content_maintype() != "image":
            continue

        payload = part.get_payload(decode=True)
        if not payload:
            continue

        try:
            with Image.open(BytesIO(payload)) as im:
                im = ImageOps.exif_transpose(im)
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

                im = im.resize((new_width, new_height), Image.LANCZOS)
                out = BytesIO()
                im.save(out, format="JPEG", quality=100)
                jpeg_bytes = out.getvalue()
        except Exception as e:
            print("Failed to resize: " + str(e))
            continue

        part.set_payload(jpeg_bytes)
        part.replace_header("Content-Type", "image/jpeg")
        if "Content-Transfer-Encoding" in part:
            del part["Content-Transfer-Encoding"]
        encoders.encode_base64(part)

        filename = part.get_filename() or ""
        if filename:
            if "." in filename:
                filename = filename.rsplit(".", 1)[0]
            filename = f"{filename}.jpg"
            part.set_param("filename", filename, header="Content-Disposition", replace=True)


def _sign_unsub(email_addr: str, token: str) -> str:
    msg = f"{email_addr}\n{token}".encode("utf-8")
    return hmac.new(TOKEN_SECRET, msg, hashlib.sha256).hexdigest()


def _build_unsub_link(email_addr: str, token: str) -> str:
    qs = urlencode({"e": email_addr, "t": token, "s": _sign_unsub(email_addr, token)})
    return f"{PUBLIC_BASE_URL}{UNSUBSCRIBE_PATH}?{qs}"


def _append_unsub(msg, link: str):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() != "text":
                continue
            subtype = part.get_content_subtype()
            text = part.get_content()
            if subtype == "html":
                text = f"{text}<br><br><p>Unsubscribe: <a href=\"{link}\">{link}</a></p>"
            else:
                text = f"{text}\n\nUnsubscribe: {link}\n"
            part.set_content(text, subtype=subtype, charset=part.get_content_charset() or "utf-8")
    else:
        subtype = msg.get_content_subtype()
        text = msg.get_content()
        if subtype == "html":
            text = f"{text}<br><br><p>Unsubscribe: <a href=\"{link}\">{link}</a></p>"
        else:
            text = f"{text}\n\nUnsubscribe: {link}\n"
        msg.set_content(text, subtype=subtype, charset=msg.get_content_charset() or "utf-8")


def forward_full_fidelity(raw_bytes: bytes, rcpt: str, token: str):
    msg = BytesParser(policy=policy.SMTP).parsebytes(raw_bytes)

    # Minimal header surgery (preserves MIME parts/attachments)
    _resize_inline_images(msg)
    # 1) Ensure single recipient in To:
    set_or_replace(msg, "To", rcpt)

    # 2) Your visible From (can also keep original if you prefer)
    set_or_replace(msg, "From", FROM_HEADER)

    # 3) Reply-To behavior
    if REPLY_TO_MODE == "original":
        # if original From exists in message (it should), keep Reply-To to original sender
        # if you'd rather force replies elsewhere, set REPLY_TO_MODE="list"
        pass
    else:
        set_or_replace(msg, "Reply-To", FROM_HEADER)

    # Optional: List headers (helps legit mailing list semantics)
    # Note: you said you already append an unsubscribe link; keep your existing mechanism here.
    # set_or_replace(msg, "List-ID", "Your List <list.yourdomain.com>")

    unsub_link = _build_unsub_link(rcpt, token)
    set_or_replace(msg, "List-Unsubscribe", f"<{unsub_link}>")
    _append_unsub(msg, unsub_link)
    data = msg.as_bytes(policy=policy.SMTP)
    return data


def forward_test_message(raw_bytes: bytes, sender: str) -> bytes:
    msg = BytesParser(policy=policy.SMTP).parsebytes(raw_bytes)

    _resize_inline_images(msg)

    set_or_replace(msg, "To", sender)
    set_or_replace(msg, "From", FROM_HEADER)

    subj = msg.get("Subject")
    if subj:
        set_or_replace(msg, "Subject", f"[TEST] {subj}")
    else:
        set_or_replace(msg, "Subject", "[TEST]")

    data = msg.as_bytes(policy=policy.SMTP)
    return data


def main_loop():
    imap = connect_imap()
    smtp = connect_smtp()
    try:
        # Search all messages; filter in code for allowed sender or bounces
        status, data = imap.search(None, "ALL")
        if status != "OK":
            return

        ids = data[0].split() if data and data[0] else []
        for msg_id in ids:
            st, fetched = imap.fetch(msg_id, "(RFC822 INTERNALDATE)")
            if st != "OK" or not fetched or not fetched[0]:
                continue
            raw = fetched[0][1]
            internaldate = imaplib.Internaldate2tuple(fetched[0][0])
            if internaldate is None:
                continue
            msg_dt = datetime.fromtimestamp(time.mktime(internaldate), tz=timezone.utc)

            con = sqlite3.connect(DB_PATH)
            cur = con.cursor()
            last_seen_raw = _get_config_value(cur, "last_processed_at")
            last_seen = datetime.fromisoformat(last_seen_raw) if last_seen_raw else None
            if last_seen and msg_dt <= last_seen:
                con.close()
                continue

            msg = BytesParser(policy=policy.SMTP).parsebytes(raw)
            from_hdr = (msg.get("From") or "").lower()
            is_test = _has_test_tag(_extract_header_recipients(msg))
            is_bounce = _is_bounce(msg)
            if not any(v in from_hdr for v in ALLOWED_FROMS) and not is_bounce:
                con.close()
                continue

            print(f"Received message {msg_id.decode() if hasattr(msg_id, 'decode') else msg_id}: {msg.get('subject')}")
            contacts = load_contacts()
            _set_config_value(cur, "last_processed_at", msg_dt.isoformat())

            if is_bounce:
                bounced = _extract_bounce_recipients(msg)
                for email in bounced:
                    now = datetime.now(timezone.utc).isoformat()
                    cur.execute(
                        "UPDATE recipients SET unsubscribed=1, unsubscribed_at=?, updated_at=? WHERE email=?",
                        (now, now, email),
                    )
                    print(f"Auto-unsubscribed bounce: {email}")
                con.commit()
                con.close()
                imap.store(msg_id, "+FLAGS", "\\Seen")
                continue

            if is_test:
                sender = _extract_sender_email(msg)
                if sender:
                    print(f"Test recipient detected; relaying only to sender {sender}")
                    mime_bytes = forward_test_message(raw, sender)
                    smtp.sendmail(SMTP_USER, [sender], mime_bytes)
                else:
                    print("Test recipient detected but no sender address found; skipping")
                con.commit()
                con.close()
                imap.store(msg_id, "+FLAGS", "\\Seen")
                time.sleep(random.uniform(*PER_MESSAGE_SLEEP_RANGE))
                continue

            # Send in priority order
            for _rank, rcpt, token in contacts:
                print(f"Sending to {rcpt}")
                mime_bytes = forward_full_fidelity(raw, rcpt, token)

                # Envelope sender can differ from header From:
                smtp.sendmail(SMTP_USER, [rcpt], mime_bytes)

                time.sleep(random.uniform(*PER_RCPT_SLEEP_RANGE))

            con.commit()
            con.close()

            # Mark as seen (optional)
            imap.store(msg_id, "+FLAGS", "\\Seen")

            time.sleep(random.uniform(*PER_MESSAGE_SLEEP_RANGE))
    except Exception as e:
        print("Exception: " + str(e))
    finally:
        try: smtp.quit()
        except Exception: pass
        try: imap.logout()
        except Exception: pass


if __name__ == "__main__":
    while True:
        try:
            main_loop()
        except Exception:
            pass
        time.sleep(POLL_SECONDS)
