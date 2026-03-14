#!/usr/bin/env python3

import imaplib
import ssl
import smtplib
import time
import random
from email import policy, encoders
from email.parser import BytesParser
from email.utils import getaddresses
from io import BytesIO
import sqlite3
import hmac
import hashlib
import re
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone
from config import load_config


app_config = load_config()

TEST_TAG = "+test"
MAX_MESSAGE_AGE = timedelta(minutes=15)

REPLY_TO_MODE = "original"  # "original" or "list"
INLINE_IMAGE_WIDTH = 600


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


def main_loop():
    imap = connect_imap()
    con = None
    try:
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
                _set_config_value(cur, "last_uid", uid_text)
                con.commit()
                con.close()
                con = None
                continue

            if is_stale:
                print(f"Skipping stale message from {msg_dt.isoformat()}")
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
                if sender:
                    print(f"Test recipient detected; relaying only to sender {sender}")
                    mime_bytes = forward_test_message(raw, sender)
                    smtp = connect_smtp()
                    try:
                        smtp.sendmail(app_config.smtp.username, [sender], mime_bytes)
                    except Exception as e:
                        print(f"Failed to send test mail: {str(e)}")
                    finally:
                        smtp.quit()
                else:
                    print("Test recipient detected but no sender address found; skipping")
                time.sleep(random.uniform(*app_config.relay.per_message_sleep_seconds))
                continue

            # Send in priority order
            sent = 0
            for _rank, rcpt, token in contacts:
                print(f"Sending to {rcpt}")

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
                sent += 1
                if sent % app_config.relay.batch_size == 0:
                    time.sleep(random.uniform(*app_config.relay.between_batches_sleep_seconds))

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
