# -*- coding: utf-8 -*-
"""邮箱相关 HTTP 接口，以及前端接线。"""
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import config
from app.core import mailfetch, mailbox, repo
from app.server.api import app
from tests.support import TempDataTest, purchase_client
from tests.test_mailbox import FakeClient, build_mail

ROOT = Path(__file__).resolve().parent.parent


class MailApiCase(TempDataTest):

    def setUp(self):
        super().setUp()
        self.client = purchase_client(self)

    def seed_pending(self, subject='电子发票', filename='invoice.pdf'):
        """造一条待归档记录（直接调 core，不经过 HTTP）。"""
        cfg = mailbox.MailConfig(host='imap.example.com', user='a@example.com',
                                 password='x', days=0)
        r = mailfetch.collect(cfg, client=FakeClient(
            [('1', build_mail(subject=subject, filename=filename).as_bytes())]))
        self.assertTrue(r['ok'], r['message'])
        rows = repo.list_mail_attachments(status=repo.MAIL_PENDING)
        self.assertTrue(rows)
        return rows[0]


class TestPresets(MailApiCase):

    def test_lists_providers(self):
        got = self.client.get('/api/mail/presets').json()
        names = [p['name'] for p in got]
        for name in ('QQ邮箱', '163邮箱', '腾讯企业邮箱', '自定义'):
            self.assertIn(name, names)
        self.assertTrue(all(p['port'] == 993 for p in got))


class TestConfigApi(MailApiCase):

    def test_default_has_no_password_key(self):
        got = self.client.get('/api/mail/config').json()
        self.assertNotIn('password', got)
        self.assertFalse(got['has_password'])
        self.assertEqual(got['host'], '')

    def test_save_then_read_back(self):
        r = self.client.put('/api/mail/config', json={
            'host': 'imap.qq.com', 'user': 'a@qq.com', 'password': '授权码',
            'days': 7, 'auto_interval': 30, 'keywords': ['发票', '报销'],
        })
        self.assertEqual(r.status_code, 200, r.text)
        got = self.client.get('/api/mail/config').json()
        self.assertEqual(got['host'], 'imap.qq.com')
        self.assertEqual(got['user'], 'a@qq.com')
        self.assertEqual(got['days'], 7)
        self.assertEqual(got['auto_interval'], 30)
        self.assertEqual(got['keywords'], ['发票', '报销'])
        self.assertTrue(got['has_password'])
        self.assertNotIn('password', got, '授权码绝不能回传给前端')

    def test_password_kept_when_omitted(self):
        self.client.put('/api/mail/config', json={'host': 'imap.qq.com', 'password': 'secret'})
        self.client.put('/api/mail/config', json={'user': 'a@qq.com'})     # 这次没带 password
        cfg = mailbox.load_config()
        self.assertEqual(cfg.password, 'secret', '没动授权码框就不该覆盖')
        self.assertEqual(cfg.user, 'a@qq.com')

    def test_password_on_disk_is_obfuscated(self):
        self.client.put('/api/mail/config', json={'host': 'imap.qq.com', 'password': 'verysecret'})
        raw = config.MAIL_CONFIG.read_text(encoding='utf-8')
        self.assertNotIn('verysecret', raw, '配置文件里不能有明文授权码')

    def test_keywords_are_cleaned(self):
        self.client.put('/api/mail/config', json={'keywords': ['发票', '  ', '']})
        self.assertEqual(mailbox.load_config().keywords, ['发票'])


class TestTestAndFetch(MailApiCase):

    def test_test_connection_without_config(self):
        r = self.client.post('/api/mail/test')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['ok'])

    def test_fetch_without_config_is_not_an_error(self):
        r = self.client.post('/api/mail/fetch')
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertFalse(j['ok'])
        self.assertIn('还没配置', j['message'])
        self.assertTrue(j['lines'])
        self.assertNotIn('attachments', j, '附件原文不该进响应')


class TestPendingApi(MailApiCase):

    def test_empty(self):
        self.assertEqual(self.client.get('/api/mail/pending').json(), [])

    def make_round_with_item(self, paid=6200):
        rid = self.client.post('/api/rounds', json={'name': '八月采购'}).json()['id']
        mid = self.client.post('/api/members', json={'name': '张三'}).json()['id']
        body = {'member_id': mid, 'name': '充电机', 'unit': '个', 'qty': 1, 'price': 6200,
                'purpose': '用', 'applicant': '张三', 'group_name': '电气组'}
        if paid is not None:
            body['paid'] = paid
        iid = self.client.post('/api/rounds/%d/items' % rid, json=body).json()['item']['id']
        return rid, iid

    def test_lists_with_suggestions(self):
        rid, _ = self.make_round_with_item()
        self.seed_pending(subject='电子发票 ¥6200.00')
        got = self.client.get('/api/mail/pending?round_id=%d' % rid).json()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]['status'], repo.MAIL_PENDING)
        self.assertEqual(got[0]['suggestions'][0]['name'], '充电机')

    def test_without_round_no_suggestions(self):
        self.make_round_with_item()
        self.seed_pending(subject='电子发票 ¥6200.00')
        self.assertEqual(self.client.get('/api/mail/pending').json()[0]['suggestions'], [])


class TestAttachApi(MailApiCase):

    def setUp(self):
        super().setUp()
        rid = self.client.post('/api/rounds', json={'name': '八月采购'}).json()['id']
        mid = self.client.post('/api/members', json={'name': '张三'}).json()['id']
        self.iid = self.client.post('/api/rounds/%d/items' % rid, json={
            'member_id': mid, 'name': '充电机', 'unit': '个', 'qty': 1, 'price': 6200,
            'purpose': '用', 'applicant': '张三', 'group_name': '电气组',
            'paid': 6200}).json()['item']['id']

    def test_attach_archives_it(self):
        row = self.seed_pending()
        r = self.client.post('/api/mail/attach', json={'mail_id': row['id'], 'item_id': self.iid})
        self.assertEqual(r.status_code, 200, r.text)
        j = r.json()
        self.assertEqual(j['stored_name'], 'DQ-充电机-6200.pdf')
        self.assertEqual(j['item']['invoice_status'], '已收到')
        self.assertTrue(j['invoice']['rel_path'].startswith('invoices/八月采购/'))

        after = self.client.get('/api/mail/pending').json()[0]
        self.assertEqual(after['status'], 'attached')
        self.assertEqual(after['suggestions'], [])

    def test_attach_twice_is_400(self):
        row = self.seed_pending()
        body = {'mail_id': row['id'], 'item_id': self.iid}
        self.client.post('/api/mail/attach', json=body)
        self.assertEqual(self.client.post('/api/mail/attach', json=body).status_code, 400)

    def test_attach_missing_item_is_400(self):
        row = self.seed_pending()
        r = self.client.post('/api/mail/attach', json={'mail_id': row['id'], 'item_id': 9999})
        self.assertEqual(r.status_code, 400)
        self.assertIn('条目不存在', r.json()['detail'])

    def test_ignore_removes_it(self):
        row = self.seed_pending()
        self.assertEqual(self.client.delete('/api/mail/pending/%d' % row['id']).status_code, 200)
        self.assertEqual(self.client.get('/api/mail/pending').json(), [])

    def test_ignore_missing_is_404(self):
        self.assertEqual(self.client.delete('/api/mail/pending/9999').status_code, 404)


class TestFrontendWiring(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / 'app/web/index.html').read_text(encoding='utf-8')
        cls.js = (ROOT / 'app/web/app.js').read_text(encoding='utf-8')

    def test_controls_exist(self):
        for needle in ('id="mailTest"', 'id="mailFetch"', 'id="mailSave"',
                       'id="m_host"', 'id="m_user"', 'id="m_password"', 'id="m_preset"',
                       'id="mailPending"', 'id="mailMsg"'):
            self.assertIn(needle, self.html, 'index.html 缺 %s' % needle)

    def test_buttons_are_bound(self):
        for fn in ('saveMailConfig', 'mailTest', 'mailFetch'):
            self.assertIn("addEventListener('click', %s)" % fn, self.js)

    def test_points_at_the_endpoints(self):
        for path in ("'/api/mail/presets'", "'/api/mail/config'", "'/api/mail/test'",
                     "'/api/mail/fetch'", "'/api/mail/pending'", "'/api/mail/attach'"):
            self.assertIn(path, self.js)

    def test_password_is_never_written_back_to_the_input(self):
        self.assertNotIn("$('m_password').value = c.password", self.js)
        self.assertIn("$('m_password').value = ''", self.js)

    def test_loaded_on_startup(self):
        self.assertIn('await loadMailConfig();', self.js)
        self.assertIn('await loadPending();', self.js)
        self.assertIn('await loadMailPresets();', self.js)

    def test_page_explains_the_authorization_code(self):
        self.assertIn('授权码不是登录密码', self.html)


if __name__ == '__main__':
    unittest.main(verbosity=2)
