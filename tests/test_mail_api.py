# -*- coding: utf-8 -*-
"""邮箱相关 HTTP 接口，以及前端接线。"""
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import config
from app.core import mailfetch, mailbox, repo
from app.core.db import session
from app.server.api import app
from tests.support import TempDataTest, purchase_client
from tests.test_mailbox import FakeClient, build_mail

ROOT = Path(__file__).resolve().parent.parent


class MailApiCase(TempDataTest):

    def setUp(self):
        super().setUp()
        self.client = purchase_client(self)

    def seed_pending(self, subject='电子发票', filename='invoice.pdf'):
        """造一条待归档记录（直接调 core，不经过 HTTP）。

        `auto_attach=False`：这些用例要的是「票留在待归档、并给出建议」，
        自动挂会把它直接挂走（那条逻辑由 tests/test_auto_attach.py 专门覆盖）。
        """
        cfg = mailbox.MailConfig(host='imap.example.com', user='a@example.com',
                                 password='x', days=0, auto_attach=False)
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


class TestPendingFileApi(MailApiCase):
    """待归档的发票要能**先看再挂** —— 看不到票面就不知道该挂哪一笔。

    归档后的票有 /api/invoices/{id}/file，但待归档的还没进 invoices 表，
    得另开一条，否则「待归档」里的票谁都打不开。
    """

    def setUp(self):
        super().setUp()
        self.row = self.seed_pending()

    def test_inline_by_default(self):
        r = self.client.get('/api/mail/pending/%d/file' % self.row['id'])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers['content-type'], 'application/pdf')
        self.assertIn('inline', r.headers.get('content-disposition', ''))
        self.assertEqual(r.content, b'%PDF-1.4 fake invoice')

    def test_download_asks_for_an_attachment(self):
        r = self.client.get('/api/mail/pending/%d/file?download=1' % self.row['id'])
        self.assertEqual(r.status_code, 200)
        self.assertIn('attachment', r.headers.get('content-disposition', ''))

    def test_a_missing_record_is_404(self):
        self.assertEqual(self.client.get('/api/mail/pending/9999/file').status_code, 404)

    def test_a_vanished_file_is_404_not_a_crash(self):
        (config.DATA_DIR / self.row['rel_path']).unlink()
        self.assertEqual(self.client.get('/api/mail/pending/%d/file' % self.row['id']).status_code,
                         404)

    def test_a_path_outside_the_data_dir_is_refused(self):
        """rel_path 万一被改过，不能让它读到 data/ 之外去。"""
        with session() as c:
            c.execute('UPDATE mail_inbox SET rel_path = ? WHERE id = ?',
                      ('../../etc/passwd', self.row['id']))
        r = self.client.get('/api/mail/pending/%d/file' % self.row['id'])
        self.assertEqual(r.status_code, 400)

    def test_anonymous_is_401(self):
        from app.server.api import app as the_app
        anon = TestClient(the_app)
        self.assertEqual(anon.get('/api/mail/pending/%d/file' % self.row['id']).status_code, 401)


class TestPendingCardWiring(unittest.TestCase):
    """卡片上得有「看发票」/「下载」，否则又回到「看不到票就挂不了」。"""

    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / 'app/web/app.js').read_text(encoding='utf-8')

    def test_the_card_links_to_the_pending_file(self):
        self.assertIn("openFileViewer('/api/mail/pending/' + mid + '/file'", self.js)

    def test_it_offers_both_look_and_download(self):
        self.assertIn("look.textContent = '看发票'", self.js)
        self.assertIn("save.textContent = '下载'", self.js)
        self.assertIn("/file?download=1", self.js)

    def test_they_are_actually_appended_to_the_row(self):
        """写了控件忘了 append 就等于没加 —— 这个坑真踩过。"""
        self.assertIn('row2.append(look, save, sel, go, del)', self.js)

    def test_the_attached_card_points_at_the_archived_invoice(self):
        """挂完待归档那份文件就删了，得指向归档后的票，不然点开 404。"""
        self.assertIn("openFileViewer('/api/invoices/' + invId + '/file'", self.js)

    def test_look_opens_the_in_page_viewer_not_a_new_tab(self):
        """新标签页关起来太别扭（用户反馈过）—— 必须是页面内弹层。"""
        self.assertIn('function openFileViewer(', self.js)
        self.assertIn('viewPendingFile(r.id, r.stored_name)', self.js)
        self.assertIn('viewInvoiceFile(r.invoice_id, r.stored_name)', self.js)
        # 不能再走 target=_blank 那条路
        for seg in self.js.split('look.textContent')[1:]:
            self.assertNotIn("look.target = '_blank'", seg.split('save.textContent')[0])

    def test_the_viewer_can_be_closed_three_ways(self):
        self.assertIn("$('fileViewerClose').addEventListener('click', closeFileViewer)", self.js)
        self.assertIn('closeFileViewer()', self.js)
        self.assertIn("e.key === 'Escape'", self.js)

    def test_the_viewer_container_exists(self):
        html = (ROOT / 'app/web/index.html').read_text(encoding='utf-8')
        for needle in ('id="fileViewer"', 'id="fileViewerFrame"', 'id="fileViewerTitle"',
                       'id="fileViewerClose"', 'id="fileViewerDownload"'):
            self.assertIn(needle, html, 'index.html 缺 %s' % needle)


if __name__ == '__main__':
    unittest.main(verbosity=2)


class TestTheSwitchesActuallySave(MailApiCase):
    """**前端那些勾选框必须真的存进配置**。

    踩过的坑：`MailConfigIn` 里没声明某个字段时，请求里的它会被**静默丢掉** ——
    保存返回 200、界面看着也勾着，但后端一直是默认值，用户以为开了其实没开。
    所以每个开关都要有一条「存进去再读回来」的断言。
    """

    def save_and_read(self, **flags):
        r = self.client.put('/api/mail/config', json=flags)
        self.assertEqual(r.status_code, 200, r.text)
        return self.client.get('/api/mail/config').json()

    def test_auto_attach_round_trips(self):
        self.assertFalse(self.save_and_read(auto_attach=False)['auto_attach'])
        self.assertTrue(self.save_and_read(auto_attach=True)['auto_attach'])

    def test_link_download_round_trips(self):
        self.assertFalse(self.save_and_read(link_download=False)['link_download'])
        self.assertTrue(self.save_and_read(link_download=True)['link_download'])

    def test_scan_read_round_trips(self):
        self.assertTrue(self.save_and_read(scan_read=True)['scan_read'])
        self.assertFalse(self.save_and_read(scan_read=False)['scan_read'])

    def test_the_defaults_are_the_sensible_ones(self):
        """没配过时：正文链接要抓（不然淘宝发票收不到）、自动挂开着。"""
        got = self.client.get('/api/mail/config').json()
        self.assertTrue(got['link_download'])
        self.assertTrue(got['auto_attach'])
        self.assertFalse(got['scan_read'])
