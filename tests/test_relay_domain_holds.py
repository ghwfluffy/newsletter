import json
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_relay_bounces as bounce_tests
from test_relay_images import relay
from delivery_state import block_domain, ensure_schema, hold_delivery


class DomainHoldTests(unittest.TestCase):
    setUp = bounce_tests.BounceTests.setUp
    lines = bounce_tests.BounceTests.lines
    state = bounce_tests.BounceTests.state

    def query(self, sql, args=()):
        with sqlite3.connect(self.db) as con:
            ensure_schema(con)
            return con.execute(sql, args).fetchall()

    def source(self):
        return dict(message_id='<newsletter@example.com>', uid='2603', uidvalidity='1')

    def configure_delivery(self):
        self.config.imap = SimpleNamespace(normalized_filter_recipient='sender@example.com')
        self.config.relay.per_recipient_sleep_seconds = (0, 0)
        self.config.relay.between_batches_sleep_seconds = (0, 0)
        self.config.relay.batch_size = 50
        imap = Mock()
        imap.response.return_value = ('UIDVALIDITY', [b'1'])
        imap.uid.return_value = ('OK', [(b'metadata', (
            b'From: sender@example.com\r\nMessage-ID: <newsletter@example.com>\r\n\r\nBody'
        ))])
        return imap

    def test_policy_bounce_blocks_and_holds_idempotently(self):
        self.logs.joinpath('mail.log').write_text(''.join(self.lines('5.7.1', '550 IP on our block list (S3140)')))
        self.assertEqual(relay._scan_postfix_bounces(), 0)
        self.assertEqual(self.query('SELECT domain FROM domain_blocks WHERE released_at IS NULL'), [('example.com',)])
        self.assertEqual(self.query('SELECT message_id,recipient_id FROM held_deliveries'), [('<newsletter@example.com>', 1)])
        self.assertEqual(self.state()[0], 0)
        relay._scan_postfix_bounces()
        self.assertEqual(self.query('SELECT count(*) FROM held_deliveries'), [(1,)])
        self.assertEqual(self.query('SELECT count(*) FROM provider_failure_events'), [(1,)])

    def test_old_event_does_not_restore_released_hold(self):
        self.logs.joinpath('mail.log').write_text(''.join(self.lines('5.7.1', '550 Spam policy rejection')))
        relay._scan_postfix_bounces()
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE domain_blocks SET released_at='2026-10-01T00:00:00+00:00'")
        relay._scan_postfix_bounces()
        self.assertEqual(self.query('SELECT domain FROM domain_blocks WHERE released_at IS NULL'), [])
        self.assertEqual(self.query('SELECT count(*) FROM held_deliveries'), [(1,)])
        new_lines = ''.join(self.lines('5.7.1', '550 Spam policy rejection')).replace('ABC123', 'NEW123').replace('2026-09-30T19:17:30', '2026-10-01T00:17:30')
        self.logs.joinpath('mail.log').write_text(new_lines)
        relay._scan_postfix_bounces()
        self.assertEqual(self.query('SELECT domain FROM domain_blocks WHERE released_at IS NULL'), [('example.com',)])

    def test_deferred_provider_mail_stays_in_postfix_without_duplicate_retry(self):
        self.logs.joinpath('mail.log').write_text(''.join(self.lines('4.7.0', '421 Messages deferred due to user complaints - TSS04', 'deferred')))
        relay._scan_postfix_bounces()
        self.assertEqual(self.query('SELECT domain FROM domain_blocks WHERE released_at IS NULL'), [('example.com',)])
        self.assertEqual(self.query('SELECT count(*) FROM held_deliveries'), [(0,)])

    def test_later_success_clears_historical_policy_failure(self):
        failed = ''.join(self.lines('5.7.1', '550 Spam block'))
        success = ''.join(self.lines('2.0.0', '250 OK', 'sent')).replace('ABC123', 'NEW123').replace('19:17:30', '19:18:30')
        self.logs.joinpath('mail.log').write_text(failed + success)
        relay._scan_postfix_bounces()
        self.assertEqual(self.query('SELECT count(*) FROM held_deliveries'), [(0,)])
        relay._scan_postfix_bounces()
        self.assertEqual(self.query('SELECT count(*) FROM held_deliveries'), [(0,)])

    def test_replay_moves_blocked_recipient_to_persistent_hold(self):
        imap = self.configure_delivery()
        request = dict(**self.source(), recipient_ids=[1], total_count=1, sent_count=0, skipped_count=0)
        with sqlite3.connect(self.db) as con:
            ensure_schema(con)
            block_domain(con, 'example.com', 'Manual hold')
            con.execute("UPDATE config SET value=? WHERE key='pending_replay'", (json.dumps(request),))
            con.execute("INSERT INTO config VALUES ('last_uid','2603')")
        with patch.object(relay, '_check_bounces'), patch.object(relay, 'connect_smtp') as smtp:
            self.assertTrue(relay._process_pending_replay(imap))
            smtp.assert_not_called()
        self.assertEqual(self.query('SELECT count(*) FROM held_deliveries'), [(1,)])
        self.assertEqual(self.query("SELECT value FROM config WHERE key='last_uid'"), [('2603',)])
        self.assertEqual(self.query("SELECT value FROM config WHERE key='pending_replay'"), [])

    def test_release_retries_only_active_recipient_and_removes_after_acceptance(self):
        imap = self.configure_delivery()
        with sqlite3.connect(self.db) as con:
            ensure_schema(con)
            hold_delivery(con, self.source(), 1)
            block_domain(con, 'example.com', 'Manual hold')
        with patch.object(relay, '_check_bounces'), patch.object(relay, 'connect_smtp') as connect, \
                patch.object(relay, 'forward_full_fidelity', return_value=b'rebuilt'):
            self.assertFalse(relay._process_held_deliveries(imap))
            connect.assert_not_called()
            with sqlite3.connect(self.db) as con:
                con.execute('UPDATE domain_blocks SET released_at=?', ('2026-10-01T00:00:00+00:00',))
            self.assertTrue(relay._process_held_deliveries(imap))
            connect.return_value.sendmail.assert_called_once_with('sender@example.com', ['reader@example.com'], b'rebuilt')
        self.assertEqual(self.query('SELECT count(*) FROM held_deliveries'), [(0,)])

    def test_failed_retry_keeps_job_and_source_mismatch_never_sends(self):
        imap = self.configure_delivery()
        with sqlite3.connect(self.db) as con:
            ensure_schema(con)
            hold_delivery(con, self.source(), 1)
        with patch.object(relay, '_check_bounces'), patch.object(relay, 'connect_smtp') as connect, \
                patch.object(relay, 'forward_full_fidelity', return_value=b'rebuilt'):
            connect.return_value.sendmail.side_effect = OSError
            self.assertTrue(relay._process_held_deliveries(imap))
            self.assertEqual(self.query('SELECT count(*) FROM held_deliveries'), [(1,)])
            connect.reset_mock()
            imap.response.return_value = ('UIDVALIDITY', [b'wrong'])
            relay._process_held_deliveries(imap)
            connect.assert_not_called()
            self.assertEqual(self.query('SELECT count(*) FROM held_deliveries'), [(1,)])

    def test_unsubscribed_retry_is_discarded_without_sending(self):
        imap = self.configure_delivery()
        with sqlite3.connect(self.db) as con:
            ensure_schema(con)
            hold_delivery(con, self.source(), 1)
            con.execute('UPDATE recipients SET unsubscribed=1')
        with patch.object(relay, '_check_bounces'), patch.object(relay, 'connect_smtp') as smtp:
            self.assertFalse(relay._process_held_deliveries(imap))
            smtp.assert_not_called()
        self.assertEqual(self.query('SELECT count(*) FROM held_deliveries'), [(0,)])

    def test_bounce_for_still_pending_recipient_does_not_create_second_job(self):
        request = dict(**self.source(), recipient_ids=[1])
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE config SET value=? WHERE key='pending_replay'", (json.dumps(request),))
        self.logs.joinpath('mail.log').write_text(''.join(self.lines('5.7.1', '550 Spam block')))
        relay._scan_postfix_bounces()
        self.assertEqual(self.query('SELECT count(*) FROM held_deliveries'), [(0,)])
        self.assertEqual(self.query('SELECT count(*) FROM domain_blocks WHERE released_at IS NULL'), [(1,)])

    def test_replay_holds_one_domain_and_sends_to_other_domain(self):
        imap = self.configure_delivery()
        request = dict(**self.source(), recipient_ids=[1, 2], total_count=2, sent_count=0, skipped_count=0)
        with sqlite3.connect(self.db) as con:
            ensure_schema(con)
            block_domain(con, 'example.com', 'Manual hold')
            con.execute("INSERT INTO recipients VALUES (2,'other@example.net',0,'2026-09-29',NULL,'other-token')")
            con.execute("UPDATE config SET value=? WHERE key='pending_replay'", (json.dumps(request),))
        with patch.object(relay, '_check_bounces'), patch.object(relay, 'connect_smtp') as smtp, \
                patch.object(relay, 'forward_full_fidelity', return_value=b'rebuilt'):
            relay._process_pending_replay(imap)
            smtp.return_value.sendmail.assert_called_once_with('sender@example.com', ['other@example.net'], b'rebuilt')
        self.assertEqual(self.query('SELECT recipient_id FROM held_deliveries'), [(1,)])

    def test_normal_newsletter_holds_domain_and_preserves_imap_checkpoint(self):
        import time
        imap = self.configure_delivery()
        self.config.relay.per_message_sleep_seconds = (0, 0)
        with sqlite3.connect(self.db) as con:
            ensure_schema(con)
            block_domain(con, 'example.com', 'Manual hold')
        raw = b'From: sender@example.com\r\nMessage-ID: <newsletter@example.com>\r\n\r\nBody'

        def uid(operation, *args):
            if operation == 'search':
                return 'OK', [b'2603']
            if operation == 'fetch':
                return 'OK', [(b'metadata', raw)]
            return 'OK', []
        imap.uid.side_effect = uid
        with patch.object(relay, 'connect_imap', return_value=imap), \
                patch.object(relay, '_check_bounces'), \
                patch.object(relay, '_process_domain_tests'), \
                patch.object(relay, '_process_held_deliveries'), \
                patch.object(relay, '_process_pending_replay', return_value=False), \
                patch.object(relay, 'load_contacts', return_value=[(1, 'reader@example.com', 'test-token')]), \
                patch.object(relay.imaplib, 'Internaldate2tuple', return_value=time.gmtime()), \
                patch.object(relay, 'connect_smtp') as smtp:
            relay.main_loop()
            smtp.assert_not_called()
        self.assertEqual(self.query('SELECT recipient_id FROM held_deliveries'), [(1,)])
        self.assertEqual(self.query("SELECT value FROM config WHERE key='last_uid'"), [('2603',)])
