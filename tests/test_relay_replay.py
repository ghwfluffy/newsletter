import json
from pathlib import Path
import smtplib
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_relay_images import relay


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / "list.db")
        self.request = dict(uid="2603", uidvalidity="123", message_id="<original@example.com>",
                            recipient_ids=[2, 3], total_count=2, sent_count=0, skipped_count=0)
        with sqlite3.connect(self.db) as con:
            con.executescript(
                "CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT);"
                "CREATE TABLE recipients (id INTEGER PRIMARY KEY, email TEXT, "
                "token TEXT, unsubscribed INTEGER);"
                "INSERT INTO recipients VALUES (1,'already@example.com','test',0);"
                "INSERT INTO recipients VALUES (2,'retry@example.com','test',0);"
                "INSERT INTO recipients VALUES (3,'unsubscribed@example.com','test',1);"
                "INSERT INTO config VALUES ('last_uid','2603');"
                "INSERT INTO config VALUES ('last_processed_at','2026-09-30T01:27:39+00:00');"
            )
            con.execute("INSERT INTO config VALUES ('pending_replay',?)",
                        (json.dumps(self.request),))
        config = SimpleNamespace(
            resolved_db_path=self.db, test=SimpleNamespace(enabled=False),
            imap=SimpleNamespace(normalized_filter_recipient="sender@example.com"),
            smtp=SimpleNamespace(username="sender@example.com"),
            relay=SimpleNamespace(per_recipient_sleep_seconds=(0, 0), batch_size=50,
                                  between_batches_sleep_seconds=(0, 0)),
        )
        self.addCleanup(patch.stopall)
        patch.object(relay, "app_config", config).start()
        patch.object(relay.time, "sleep").start()
        self.render = patch.object(relay, "forward_full_fidelity", return_value=b"rebuilt").start()
        self.smtp = Mock()
        self.connect = patch.object(relay, "connect_smtp", return_value=self.smtp).start()
        self.imap = Mock()
        self.imap.response.return_value = ("UIDVALIDITY", [b"123"])
        self.imap.uid.return_value = ("OK", [(b"metadata", (
            b"From: sender@example.com\r\nMessage-ID: <original@example.com>\r\n"
            b"Date: Tue, 1 Jan 2019 00:00:00 +0000\r\n\r\nOriginal message"
        ))])

    def state(self):
        with sqlite3.connect(self.db) as con:
            return dict(con.execute("SELECT key,value FROM config"))

    def test_only_selected_active_recipient_and_checkpoint_preserved(self):
        self.assertTrue(relay._process_pending_replay(self.imap))
        self.smtp.sendmail.assert_called_once_with(
            "sender@example.com", ["retry@example.com"], b"rebuilt"
        )
        self.imap.uid.assert_called_once_with("fetch", "2603", "(BODY.PEEK[])")
        state = self.state()
        self.assertNotIn("pending_replay", state)
        self.assertEqual(state["last_delivery_sent_count"], "1")
        self.assertEqual(state["last_uid"], "2603")
        self.assertEqual(state["last_processed_at"], "2026-09-30T01:27:39+00:00")

    def test_failed_submission_remains_pending(self):
        self.smtp.sendmail.side_effect = smtplib.SMTPException("failure")
        relay._process_pending_replay(self.imap)
        pending = json.loads(self.state()["pending_replay"])
        self.assertEqual(pending["recipient_ids"], [2])
        self.assertEqual(pending["sent_count"], 0)
        self.assertEqual(pending["skipped_count"], 1)

    def test_quit_failure_does_not_requeue_accepted_message(self):
        self.smtp.quit.side_effect = smtplib.SMTPServerDisconnected()
        relay._process_pending_replay(self.imap)
        self.assertNotIn("pending_replay", self.state())

    def test_wrong_mailbox_or_source_never_sends(self):
        self.imap.response.return_value = ("UIDVALIDITY", [b"999"])
        with self.assertRaises(RuntimeError):
            relay._process_pending_replay(self.imap)
        self.imap.response.return_value = ("UIDVALIDITY", [b"123"])
        self.imap.uid.return_value = ("OK", [(b"metadata", b"Message-ID: <wrong>\r\n\r\n")])
        with self.assertRaises(RuntimeError):
            relay._process_pending_replay(self.imap)
        self.connect.assert_not_called()
        self.assertEqual(json.loads(self.state()["pending_replay"]), self.request)


if __name__ == "__main__":
    unittest.main()
