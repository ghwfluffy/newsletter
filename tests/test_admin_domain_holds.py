import base64
import importlib.util
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import bcrypt


class AdminDomainHoldTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / 'list.db')
        src = Path(__file__).resolve().parents[1] / 'src'
        with sqlite3.connect(self.db) as con:
            con.executescript((src.parent / 'config/schema.sql').read_text())
        config = ModuleType('config')
        config.load_config = lambda: SimpleNamespace(
            resolved_db_path=self.db, test=SimpleNamespace(enabled=False),
            web=SimpleNamespace(admin_user='admin', admin_pass_bcrypt=bcrypt.hashpw(b'test-password', bcrypt.gensalt(rounds=4)).decode(),
                                token_secret='test-secret', public_base_url='https://localhost', domain='localhost',
                                unsubscribe_path='/unsub', manage_path='/manage', confirmation_email_enabled=False),
        )
        spec = importlib.util.spec_from_file_location('admin_tests', src / 'webserver.py')
        self.web = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'config': config}):
            spec.loader.exec_module(self.web)
        self.web.app.config['TESTING'] = True
        self.client = self.web.app.test_client()
        self.headers = {'Authorization': 'Basic ' + base64.b64encode(b'admin:test-password').decode()}

    def get(self, auth=True):
        return self.client.get('/manage', headers=self.headers if auth else {}, base_url='https://localhost')

    def csrf(self):
        return re.search(r'name="domain_csrf" value="([^"]+)"', self.get().get_data(as_text=True))[1]

    def post(self, **data):
        return self.client.post('/manage', data=data, headers=self.headers, base_url='https://localhost')

    def test_authentication_and_csrf_required(self):
        self.assertEqual(self.get(False).status_code, 401)
        self.assertEqual(self.client.post('/manage', data={'action': 'block_domain', 'domain': 'hotmail.com'},
                                          base_url='https://localhost').status_code, 401)
        self.assertEqual(self.post(action='block_domain', domain='hotmail.com').status_code, 403)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute('SELECT count(*) FROM domain_blocks').fetchone()[0], 0)

    def test_add_visible_hold_and_release_preserves_queued_message(self):
        csrf = self.csrf()
        page = self.post(action='block_domain', domain='HOTMAIL.COM', domain_csrf=csrf)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Domain delivery holds', page.data)
        self.assertIn(b'<strong>hotmail.com</strong>', page.data)
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO recipients(email,token,created_at,updated_at) VALUES ('reader@hotmail.com','secret','2026-01-01','2026-01-01')")
            con.execute("INSERT INTO held_deliveries VALUES ('<newsletter>',1,'2603','1','2026-10-01')")
        self.assertIn(b'1 messages held for retry', self.get().data)
        response = self.post(action='release_domain', domain='hotmail.com', domain_csrf=csrf)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Queued messages will retry automatically', response.data)
        with sqlite3.connect(self.db) as con:
            self.assertIsNotNone(con.execute('SELECT released_at FROM domain_blocks').fetchone()[0])
            self.assertEqual(con.execute('SELECT count(*) FROM held_deliveries').fetchone()[0], 1)

    def test_invalid_domain_is_rejected(self):
        response = self.post(action='block_domain', domain='user@example.com', domain_csrf=self.csrf())
        self.assertEqual(response.status_code, 400)

    def test_reason_is_escaped(self):
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO domain_blocks VALUES ('hotmail.com','<script>alert(1)</script>','2026-10-01',NULL)")
        page = self.get().data
        self.assertIn(b'&lt;script&gt;alert(1)&lt;/script&gt;', page)
        self.assertNotIn(b'<script>alert(1)</script>', page)

    def test_domain_test_is_authenticated_queued_once_and_does_not_release_hold(self):
        csrf = self.csrf()
        self.post(action='block_domain', domain='hotmail.com', domain_csrf=csrf)
        self.assertEqual(self.post(action='test_domain', domain='hotmail.com').status_code, 403)
        for _ in range(2):
            result = self.post(action='test_domain', domain='hotmail.com', domain_csrf=csrf)
            self.assertEqual(result.status_code, 200)
            self.assertIn(b'Test one queued message', result.data)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute('SELECT count(*) FROM domain_tests').fetchone()[0], 1)
            self.assertIsNone(con.execute('SELECT released_at FROM domain_blocks').fetchone()[0])

    def test_test_result_is_visible_and_escaped(self):
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO domain_blocks VALUES ('hotmail.com','Manual','2026-10-01',NULL)")
            con.execute("INSERT INTO domain_tests(domain,created_at,status,result) VALUES ('hotmail.com','2026-10-01','bounced',?)",
                        ('Mailbox does not exist; recipient unsubscribed <script>',))
        response = self.get()
        self.assertIn(b'Mailbox does not exist; recipient unsubscribed &lt;script&gt;', response.data)
