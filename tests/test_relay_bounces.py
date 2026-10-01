import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from unittest.mock import Mock

from test_relay_images import relay


class BounceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.logs = Path(self.temp.name)
        self.db = str(self.logs / 'list.db')
        with sqlite3.connect(self.db) as con:
            con.executescript(
                "CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT);"
                "INSERT INTO config VALUES ('domain_policy_started_at','2026-09-29T00:00:00+00:00');"
                "CREATE TABLE recipients (id INTEGER PRIMARY KEY, email TEXT, "
                "unsubscribed INTEGER, updated_at TEXT, unsubscribed_at TEXT, token TEXT);"
                "INSERT INTO recipients VALUES (1,'reader@example.com',0,"
                "'2026-09-29T00:00:00+00:00',NULL,'test-token');"
            )
            con.execute('INSERT INTO config VALUES (?,?)',
                        ('pending_replay', json.dumps({'message_id': '<newsletter@example.com>',
                                                      'uid': '2603', 'uidvalidity': '1', 'recipient_ids': []})))
        config = SimpleNamespace(
            resolved_db_path=self.db, test=SimpleNamespace(enabled=False),
            smtp=SimpleNamespace(username='sender@example.com'),
            relay=SimpleNamespace(postfix_log_dir=str(self.logs)),
        )
        patcher = patch.object(relay, 'app_config', config)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = config
        self.logs.joinpath('mail.log').write_text('')

    def lines(self, dsn='5.1.1', diagnostic='550 User does not exist',
              status='bounced', message_id='<newsletter@example.com>',
              sender='sender@example.com', recipient='reader@example.com'):
        prefix = '2026-09-30T19:17:30.123456+00:00 newsmail postfix/'
        return [
            f'{prefix}cleanup[1]: ABC123: message-id={message_id}\n',
            f'{prefix}qmgr[2]: ABC123: from=<{sender}>, size=100, nrcpt=1 (queue active)\n',
            f'{prefix}smtp[3]: ABC123: to=<{recipient}>, relay=mx.example.com[1.2.3.4]:25, '
            f'delay=1, delays=0/0/0/1, dsn={dsn}, status={status} (host mx.example.com '
            f'said: {diagnostic} (in reply to RCPT TO command))\n',
            f'{prefix}qmgr[2]: ABC123: removed\n',
        ]

    def state(self):
        with sqlite3.connect(self.db) as con:
            return con.execute('SELECT unsubscribed,updated_at,unsubscribed_at FROM recipients').fetchone()

    def test_permanent_failures_and_duplicate_scan(self):
        for dsn, diagnostic in [('5.1.1', '550 User does not exist'),
                                ('5.2.1', '550 Mailbox disabled'),
                                ('5.0.0', '554 30 This mailbox is disabled (554.30).')]:
            with self.subTest(dsn=dsn):
                with sqlite3.connect(self.db) as con:
                    con.execute("UPDATE recipients SET unsubscribed=0,updated_at='2026-09-29T00:00:00+00:00'")
                self.logs.joinpath('mail.log').write_text(''.join(self.lines(dsn, diagnostic)))
                self.assertEqual(relay._scan_postfix_bounces(), 1)
                state = self.state()
                self.assertEqual(state[0], 1)
                self.assertEqual(state[1], state[2])
                self.assertEqual(relay._scan_postfix_bounces(), 0)

    def test_temporary_policy_full_and_unrecognized_failures_are_preserved(self):
        for dsn, diagnostic, status in [
            ('4.2.1', '450 Mailbox disabled', 'deferred'),
            ('5.7.1', '550 IP blocked for spam', 'bounced'),
            ('5.1.1', '550 Sender blocked by policy', 'bounced'),
            ('5.2.2', '552 Mailbox full', 'bounced'),
            ('5.0.0', '550 Message rejected', 'bounced'),
            ('2.0.0', '250 OK', 'sent'),
        ]:
            with self.subTest(dsn=dsn, diagnostic=diagnostic):
                self.logs.joinpath('mail.log').write_text(''.join(self.lines(dsn, diagnostic, status)))
                self.assertEqual(relay._scan_postfix_bounces(), 0)
                self.assertEqual(self.state()[0], 0)

    def test_requires_newsletter_sender_and_known_recipient(self):
        for kwargs in [{'message_id': '<unrelated@example.com>'},
                       {'sender': 'other@example.com'},
                       {'recipient': 'unknown@example.com'}]:
            with self.subTest(kwargs=kwargs):
                self.logs.joinpath('mail.log').write_text(''.join(self.lines(**kwargs)))
                self.assertEqual(relay._scan_postfix_bounces(), 0)
                self.assertEqual(self.state()[0], 0)
        self.logs.joinpath('mail.log').write_text(self.lines()[2])
        self.assertEqual(relay._scan_postfix_bounces(), 0)

    def test_new_subscription_is_not_reversed_by_old_bounce(self):
        self.logs.joinpath('mail.log').write_text(''.join(self.lines()))
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE recipients SET updated_at='2026-10-01T00:00:00+00:00'")
        self.assertEqual(relay._scan_postfix_bounces(), 0)
        self.assertEqual(self.state()[0], 0)

    def test_rotation_and_completed_replay_identity(self):
        relay._remember_newsletter('<newsletter@example.com>')
        with sqlite3.connect(self.db) as con:
            con.execute("DELETE FROM config WHERE key='pending_replay'")
        lines = self.lines()
        self.logs.joinpath('mail.log.1').write_text(''.join(lines[:2]))
        self.logs.joinpath('mail.log').write_text(''.join(lines[2:]))
        self.assertEqual(relay._scan_postfix_bounces(), 1)

    def test_disabled_and_test_mode_never_modify_recipients(self):
        self.logs.joinpath('mail.log').write_text(''.join(self.lines()))
        self.config.test.enabled = True
        self.assertEqual(relay._scan_postfix_bounces(), 0)
        self.config.test.enabled = False
        self.config.relay.postfix_log_dir = ''
        self.assertEqual(relay._scan_postfix_bounces(), 0)
        self.assertEqual(self.state()[0], 0)

    def test_scan_failure_does_not_stop_relay_and_is_rate_limited(self):
        with patch.object(relay, '_last_bounce_scan', None), \
                patch.object(relay, '_scan_postfix_bounces', side_effect=OSError) as scan, \
                patch.object(relay.time, 'monotonic', side_effect=[0, 30, 60]):
            relay._check_bounces()
            relay._check_bounces()
            relay._check_bounces()
            self.assertEqual(scan.call_count, 2)

    def test_replay_skips_newly_bounced_recipient_and_preserves_checkpoint(self):
        request = dict(message_id='<newsletter@example.com>', uid='2603',
                       uidvalidity='1', recipient_ids=[1], total_count=1,
                       sent_count=0, skipped_count=0)
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE config SET value=? WHERE key='pending_replay'",
                        (json.dumps(request),))
            con.execute("INSERT INTO config VALUES ('last_uid','2603')")
        self.logs.joinpath('mail.log').write_text(''.join(self.lines()))
        self.config.imap = SimpleNamespace(normalized_filter_recipient='sender@example.com')
        imap = Mock()
        imap.response.return_value = ('UIDVALIDITY', [b'1'])
        imap.uid.return_value = ('OK', [(b'metadata', (
            b'From: sender@example.com\r\nMessage-ID: <newsletter@example.com>\r\n\r\nBody'
        ))])
        with patch.object(relay, '_last_bounce_scan', None), \
                patch.object(relay, 'connect_smtp') as smtp:
            self.assertTrue(relay._process_pending_replay(imap))
            smtp.assert_not_called()
        with sqlite3.connect(self.db) as con:
            state = dict(con.execute('SELECT key,value FROM config'))
        self.assertNotIn('pending_replay', state)
        self.assertEqual(state['last_uid'], '2603')
        self.assertEqual(state['last_delivery_sent_count'], '0')
        self.assertEqual(self.state()[0], 1)


if __name__ == '__main__':
    unittest.main()
