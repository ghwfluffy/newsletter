from datetime import datetime, timedelta, timezone
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_relay_domain_holds as holds
from test_relay_images import relay
from delivery_state import block_domain, ensure_schema, hold_delivery


class DomainTestTests(unittest.TestCase):
    setUp = holds.DomainHoldTests.setUp
    lines = holds.DomainHoldTests.lines
    state = holds.DomainHoldTests.state
    source = holds.DomainHoldTests.source
    query = holds.DomainHoldTests.query
    configure_delivery = holds.DomainHoldTests.configure_delivery

    def stage(self):
        imap = self.configure_delivery()
        self.config.web = SimpleNamespace(domain='newsmail.example.com')
        with sqlite3.connect(self.db) as con:
            ensure_schema(con)
            block_domain(con, 'example.com', 'Manual hold')
            hold_delivery(con, self.source(), 1)
            con.execute("INSERT INTO domain_tests(domain,created_at,status,result) VALUES ('example.com',?,'pending','Queued')",
                        (datetime.now(timezone.utc).isoformat(),))
        return imap

    def submit(self):
        imap = self.stage()
        with patch.object(relay, 'connect_smtp') as smtp, \
                patch.object(relay, 'forward_full_fidelity', return_value=b'From: sender@example.com\r\n\r\nBody'):
            self.assertTrue(relay._process_domain_tests(imap))
            smtp.return_value.sendmail.assert_called_once()
        self.assertEqual(self.query('SELECT status FROM domain_tests'), [('submitted',)])
        self.assertEqual(self.query('SELECT count(*) FROM held_deliveries'), [(1,)])
        return imap

    def report(self, dsn, diagnostic, status):
        identity = self.query('SELECT test_message_id FROM domain_tests')[0][0]
        timestamp = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        log = ''.join(self.lines(dsn, diagnostic, status, message_id=identity))
        log = log.replace('2026-09-30T19:17:30.123456+00:00', timestamp)
        self.logs.joinpath('mail.log').write_text(log)
        relay._scan_postfix_bounces()

    def test_local_acceptance_keeps_message_until_provider_accepts(self):
        self.submit()
        self.report('2.0.0', '250 OK', 'sent')
        self.assertEqual(self.query('SELECT status FROM domain_tests'), [('sent',)])
        self.assertEqual(self.query('SELECT count(*) FROM held_deliveries'), [(0,)])
        self.assertEqual(self.query('SELECT count(*) FROM domain_blocks WHERE released_at IS NULL'), [(1,)])
        relay._scan_postfix_bounces()
        self.assertEqual(self.query('SELECT count(*) FROM held_deliveries'), [(0,)])

    def test_nonexistent_mailbox_unsubscribes_and_reports_result(self):
        self.submit()
        self.report('5.1.1', '550 User does not exist', 'bounced')
        self.assertEqual(self.state()[0], 1)
        self.assertEqual(self.query('SELECT count(*) FROM held_deliveries'), [(0,)])
        result = self.query('SELECT result FROM domain_tests')[0][0]
        self.assertIn('recipient unsubscribed', result)
        self.assertEqual(self.query('SELECT count(*) FROM domain_blocks WHERE released_at IS NULL'), [(1,)])

    def test_provider_rejection_retains_message_and_domain_hold(self):
        self.submit()
        self.report('5.7.1', '550 IP on our block list (S3140)', 'bounced')
        self.assertEqual(self.state()[0], 0)
        self.assertEqual(self.query('SELECT status FROM domain_tests'), [('bounced',)])
        self.assertEqual(self.query('SELECT count(*) FROM held_deliveries'), [(1,)])
        self.assertEqual(self.query('SELECT count(*) FROM domain_blocks WHERE released_at IS NULL'), [(1,)])

    def test_domain_release_does_not_duplicate_an_inflight_test(self):
        imap = self.submit()
        with sqlite3.connect(self.db) as con:
            con.execute('UPDATE domain_blocks SET released_at=?', (datetime.now(timezone.utc).isoformat(),))
        with patch.object(relay, '_check_bounces'), patch.object(relay, 'connect_smtp') as smtp:
            self.assertFalse(relay._process_held_deliveries(imap))
            smtp.assert_not_called()
        self.assertEqual(self.query('SELECT count(*) FROM held_deliveries'), [(1,)])

    def test_test_can_take_one_pending_replay_recipient_without_duplicate(self):
        imap = self.stage()
        import json
        request = dict(**self.source(), recipient_ids=[1], total_count=1, sent_count=0, skipped_count=0)
        with sqlite3.connect(self.db) as con:
            con.execute('DELETE FROM held_deliveries')
            con.execute("UPDATE config SET value=? WHERE key='pending_replay'", (json.dumps(request),))
        with patch.object(relay, '_check_bounces'), patch.object(relay, 'connect_smtp') as smtp, \
                patch.object(relay, 'forward_full_fidelity', return_value=b'From: sender@example.com\r\n\r\nBody'):
            self.assertTrue(relay._process_pending_replay(imap))
            smtp.return_value.sendmail.assert_called_once()
        self.assertEqual(self.query("SELECT value FROM config WHERE key='pending_replay'"), [])
        self.assertEqual(self.query('SELECT count(*) FROM held_deliveries'), [(1,)])

    def test_failed_test_never_clears_queued_message(self):
        imap = self.stage()
        imap.response.return_value = ('UIDVALIDITY', [b'wrong'])
        with patch.object(relay, 'connect_smtp') as smtp:
            relay._process_domain_tests(imap)
            smtp.assert_not_called()
        self.assertEqual(self.query('SELECT status FROM domain_tests'), [('error',)])
        self.assertEqual(self.query('SELECT count(*) FROM held_deliveries'), [(1,)])

    def test_batch_pause_services_test_requests_without_removing_delay(self):
        self.configure_delivery()
        with patch.object(relay.random, 'uniform', return_value=65), \
                patch.object(relay.time, 'sleep') as sleep, \
                patch.object(relay, '_check_bounces'), \
                patch.object(relay, '_process_domain_tests') as tests:
            relay._batch_pause(None)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [30, 30, 5])
        self.assertEqual(tests.call_count, 3)
