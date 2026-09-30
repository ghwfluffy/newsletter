import importlib.util
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from flask import Flask


SRC = Path(__file__).resolve().parents[1] / "src"
spec = importlib.util.spec_from_file_location("subscriptions", SRC / "subscriptions.py")
subscriptions = importlib.util.module_from_spec(spec)
sys.modules["subscriptions"] = subscriptions
spec.loader.exec_module(subscriptions)


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "list.db")
        with sqlite3.connect(self.path) as con:
            con.executescript((SRC.parent / "config/schema.sql").read_text())
            con.execute("INSERT INTO config VALUES ('pending_replay','untouched')")
        self.config = SimpleNamespace(
            resolved_db_path=self.path,
            web=SimpleNamespace(token_secret="test-secret", confirmation_email_enabled=True,
                                public_base_url="https://localhost", domain="localhost"),
            smtp=SimpleNamespace(host="mail.invalid", port=25, username="sender@example.com",
                                 password=None, from_header="SPJ <sender@example.com>"),
            test=SimpleNamespace(enabled=False),
        )
        self.app = Flask("subscription-tests")
        self.app.config["TESTING"] = True
        subscriptions.register_public_routes(self.app, self.config)
        self.client = self.app.test_client()
        self.smtp = Mock()
        self.mock_smtp = patch.object(subscriptions.smtplib, "SMTP", return_value=self.smtp)
        self.mock_smtp.start()
        self.addCleanup(self.mock_smtp.stop)

    def get(self, path):
        return self.client.get(path, base_url="https://localhost")

    def post(self, path, data):
        return self.client.post(path, data=data, base_url="https://localhost")

    def csrf(self):
        page = self.get("/").get_data(as_text=True)
        return re.search(r'name="csrf" value="([^"]+)"', page)[1]

    def rows(self, sql, args=()):
        with subscriptions.database(self.config) as con:
            return con.execute(sql, args).fetchall()

    def submit(self, email="new@example.com", mode="subscribe", **extra):
        data = dict(email=email, mode=mode, consent="yes", csrf=self.csrf())
        data.update(extra)
        return self.post("/", data)

    def token(self):
        self.assertTrue(subscriptions.send_next_confirmation(self.config))
        row = self.rows("SELECT * FROM subscription_requests ORDER BY created_at DESC")[0]
        return subscriptions.confirmation_token(self.config, row)

    def existing(self, email="existing@example.com", unsubscribed=0):
        with subscriptions.database(self.config) as con:
            con.execute(
                "INSERT INTO recipients (email,rank,unsubscribed,token,created_at,updated_at) "
                "VALUES (?,7,?,'original-token','2026-01-01','2026-01-01')",
                (email, unsubscribed),
            )

    def test_pages_and_secure_form_cookie(self):
        response = self.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Send confirmation link', response.data)
        self.assertIn(b'href="/privacy"', response.data)
        self.assertIn("Secure", response.headers["Set-Cookie"])
        self.assertIn("HttpOnly", response.headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", response.headers["Set-Cookie"])
        self.assertEqual(self.get("/privacy").status_code, 200)
        self.assertIn(b"word of mouth", self.get("/privacy").data)
        with self.get("/newsletter-assets/newsletter.css") as response:
            self.assertEqual(response.status_code, 200)

    def test_signup_requires_email_and_explicit_post_confirmation(self):
        self.assertEqual(self.submit().status_code, 303)
        self.assertEqual(len(self.rows("SELECT * FROM recipients")), 0)
        token = self.token()
        self.smtp.send_message.assert_called_once()
        self.assertEqual(len(self.rows("SELECT * FROM recipients")), 0)
        page = self.get("/confirm?t=" + token)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(len(self.rows("SELECT * FROM recipients")), 0)
        self.assertEqual(page.headers["Referrer-Policy"], "no-referrer")
        response = self.post("/confirm", dict(t=token, csrf=self.csrf()))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.rows("SELECT unsubscribed FROM recipients")[0][0], 0)
        self.assertEqual(len(self.rows("SELECT * FROM subscription_events")), 1)
        self.post("/confirm", dict(t=token, csrf=self.csrf()))
        self.assertEqual(len(self.rows("SELECT * FROM subscription_events")), 1)
        self.assertEqual(self.rows("SELECT value FROM config WHERE key='pending_replay'")[0][0],
                         "untouched")

    def test_consent_validation_and_csrf(self):
        self.assertEqual(self.submit(consent="").status_code, 400)
        self.assertEqual(self.submit(email='bad\r\nBcc:bad@example.com').status_code, 400)
        self.assertEqual(self.submit(csrf="forged").status_code, 400)
        self.assertEqual(self.submit(company="bot").status_code, 303)
        self.assertEqual(len(self.rows("SELECT * FROM subscription_requests")), 0)

    def test_existing_subscribers_are_preserved(self):
        self.existing()
        before = tuple(self.rows("SELECT * FROM recipients")[0])
        self.submit(email="existing@example.com")
        self.token()
        self.assertEqual(tuple(self.rows("SELECT * FROM recipients")[0]), before)

    def test_unsubscribe_requires_confirm_and_does_not_disclose_membership(self):
        self.existing()
        unknown = self.submit(email="absent@example.com", mode="unsubscribe")
        known = self.submit(email="existing@example.com", mode="unsubscribe")
        self.assertEqual(unknown.status_code, known.status_code)
        self.assertEqual(unknown.headers["Location"], known.headers["Location"])
        self.assertEqual(len(self.rows("SELECT * FROM subscription_requests")), 1)
        token = self.token()
        self.get("/confirm?t=" + token)
        self.assertEqual(self.rows("SELECT unsubscribed FROM recipients")[0][0], 0)
        self.post("/confirm", dict(t=token, csrf=self.csrf()))
        self.assertEqual(self.rows("SELECT unsubscribed FROM recipients")[0][0], 1)

    def test_expired_or_tampered_links_do_not_subscribe(self):
        self.submit()
        token = self.token()
        self.assertEqual(self.get("/confirm?t=" + token[:-1] + "x").status_code, 400)
        with subscriptions.database(self.config) as con:
            con.execute("UPDATE subscription_requests SET expires_at=?", (int(time.time()) - 1,))
        self.assertEqual(self.get("/confirm?t=" + token).status_code, 400)
        self.assertEqual(len(self.rows("SELECT * FROM recipients")), 0)

    def test_rate_limits_and_duplicate_requests(self):
        for _ in range(3):
            self.submit()
        self.assertEqual(len(self.rows("SELECT * FROM subscription_requests")), 1)
        for i in range(9):
            self.assertEqual(self.submit(email=f"reader{i}@example.com").status_code, 303)
        self.assertEqual(self.submit(email="limit@example.com").status_code, 429)

    def test_disabled_worker_and_failed_smtp_do_not_activate(self):
        self.submit()
        self.config.web.confirmation_email_enabled = False
        self.assertFalse(subscriptions.send_next_confirmation(self.config))
        self.smtp.send_message.assert_not_called()
        self.config.web.confirmation_email_enabled = True
        self.smtp.send_message.side_effect = OSError("offline")
        self.assertFalse(subscriptions.send_next_confirmation(self.config))
        self.assertIsNone(self.rows("SELECT sent_at FROM subscription_requests")[0][0])
        self.assertEqual(len(self.rows("SELECT * FROM recipients")), 0)
        self.assertFalse(subscriptions.send_next_confirmation(self.config))
        self.assertEqual(self.smtp.send_message.call_count, 1)

    def test_request_cleanup_keeps_consent_history(self):
        self.submit()
        token = self.token()
        self.post("/confirm", dict(t=token, csrf=self.csrf()))
        with subscriptions.database(self.config) as con:
            con.execute("UPDATE subscription_requests SET created_at=?", (int(time.time()) - 8 * 86400,))
        self.submit(email="another@example.com")
        self.assertEqual(len(self.rows("SELECT * FROM subscription_requests")), 1)
        self.assertEqual(len(self.rows("SELECT * FROM subscription_events")), 1)


if __name__ == "__main__":
    unittest.main()
