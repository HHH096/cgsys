# -*- coding: utf-8 -*-
"""双入口：采购人员登录 + 后端权限边界。

这里最重要的一组断言是 **「没登录调采购专用接口必须 401」** ——
前端把按钮藏起来挡不住 F12，边界只能在服务端。
"""
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import config
from app.core import repo, settings
from app.server.api import app
from tests.support import (EntryCase, TEST_PASSWORD as PW, TEST_PDF as PDF,
                           TempDataTest)


# ================================================================== 密码与凭证

class TestSettingsUnit(TempDataTest):

    def test_path_is_computed_at_call_time(self):
        """路径必须现算 —— 写成模块常量的话，patch 了 DATA_DIR 也改不动它，
        配置就会落到真实目录（support.py 里记过这个坑）。"""
        self.assertEqual(settings.settings_file().parent, Path(config.DATA_DIR))

    def test_nothing_set_initially(self):
        self.assertFalse(settings.has_password())
        self.assertFalse(settings.check_password(''))
        self.assertFalse(settings.check_password('随便'))

    def test_set_then_check(self):
        settings.set_password(PW)
        self.assertTrue(settings.has_password())
        self.assertTrue(settings.check_password(PW))
        self.assertFalse(settings.check_password('不对'))
        self.assertFalse(settings.check_password(''))

    def test_password_is_not_plaintext_on_disk(self):
        settings.set_password(PW)
        raw = settings.settings_file().read_text(encoding='utf-8')
        self.assertNotIn(PW, raw, '密码不该明文躺在配置文件里')

    def test_clearing_password(self):
        settings.set_password(PW)
        settings.set_password('')
        self.assertFalse(settings.has_password())

    def test_token_lifecycle(self):
        token = settings.issue_token()
        self.assertTrue(settings.verify_token(token))
        settings.revoke_token(token)
        self.assertFalse(settings.verify_token(token))

    def test_bad_tokens(self):
        self.assertFalse(settings.verify_token(None))
        self.assertFalse(settings.verify_token(''))
        self.assertFalse(settings.verify_token('瞎编的'))

    def test_lockout_after_repeated_failures(self):
        ip = '1.2.3.4'
        self.assertEqual(settings.locked_for(ip), 0)
        for _ in range(settings.MAX_FAILS):
            settings.note_failure(ip)
        self.assertGreater(settings.locked_for(ip), 0)
        settings.clear_failures(ip)
        self.assertEqual(settings.locked_for(ip), 0)


# ================================================================== 登录流程

class TestLoginFlow(EntryCase):

    def test_status_before_login(self):
        st = self.anon.get('/api/purchase/status').json()
        self.assertTrue(st['has_password'])
        self.assertFalse(st['logged_in'])

    def test_wrong_password_rejected(self):
        self.assertEqual(
            self.anon.post('/api/purchase/login', json={'password': '不对'}).status_code, 401)

    def test_login_then_status(self):
        r = self.anon.post('/api/purchase/login', json={'password': PW})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.anon.get('/api/purchase/status').json()['logged_in'])

    def test_logout(self):
        self.anon.post('/api/purchase/login', json={'password': PW})
        self.anon.post('/api/purchase/logout')
        self.assertFalse(self.anon.get('/api/purchase/status').json()['logged_in'])

    def test_login_without_password_set(self):
        settings.set_password('')
        r = self.anon.post('/api/purchase/login', json={'password': 'x'})
        self.assertEqual(r.status_code, 400)

    def test_too_short_new_password(self):
        r = self.purchase.post('/api/purchase/password', json={'old': PW, 'new': '12'})
        self.assertEqual(r.status_code, 400)

    def test_change_password_needs_the_old_one(self):
        r = self.purchase.post('/api/purchase/password',
                               json={'old': '记错了', 'new': 'newpw123'})
        self.assertEqual(r.status_code, 401)

    def test_change_password_then_login_with_new(self):
        r = self.purchase.post('/api/purchase/password',
                               json={'old': PW, 'new': 'newpw123'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            self.anon.post('/api/purchase/login', json={'password': PW}).status_code, 401)
        self.assertEqual(
            self.anon.post('/api/purchase/login',
                           json={'password': 'newpw123'}).status_code, 200)

    def test_lockout_kicks_in(self):
        for _ in range(settings.MAX_FAILS):
            self.anon.post('/api/purchase/login', json={'password': 'x'})
        r = self.anon.post('/api/purchase/login', json={'password': PW})
        self.assertEqual(r.status_code, 429, '锁定之后即使密码对，也得先等着')


# ================================================================== 组员身份

class TestMemberIdentity(EntryCase):

    def test_me_returns_the_chosen_name(self):
        me = self.alice_c.get('/api/me').json()
        self.assertEqual(me['member_id'], self.alice)
        self.assertEqual(me['name'], '李四')
        self.assertFalse(me['is_purchase'])

    def test_purchase_client_is_flagged(self):
        self.assertTrue(self.purchase.get('/api/me').json()['is_purchase'])

    def test_switching_identity(self):
        self.alice_c.post('/api/me', json={'member_id': self.bob})
        self.assertEqual(self.alice_c.get('/api/me').json()['member_id'], self.bob)

    def test_clearing_identity(self):
        self.alice_c.post('/api/me', json={'member_id': None})
        self.assertIsNone(self.alice_c.get('/api/me').json()['member_id'])

    def test_unknown_member(self):
        r = self.alice_c.post('/api/me', json={'member_id': 99999})
        self.assertEqual(r.status_code, 404)


# ================================================================== 硬边界：401

class TestPurchaseOnlyEndpoints(EntryCase):
    """**没登录一律 401。** 这是整套权限里唯一的硬边界。"""

    def _must_be_401(self, path, method='get', **kw):
        for label, client in (('匿名', self.anon), ('组员', self.alice_c)):
            r = getattr(client, method)(path, **kw)
            self.assertEqual(r.status_code, 401,
                             '%s 用 %s 调 %s 应该被拒，实际 %d'
                             % (label, method.upper(), path, r.status_code))

    def test_export(self):
        self._must_be_401('/api/rounds/%d/export' % self.rid)

    def test_merged_pdf(self):
        self._must_be_401('/api/rounds/%d/merged.pdf' % self.rid)

    def test_block_and_excel(self):
        self._must_be_401('/api/rounds/%d/block' % self.rid)
        self._must_be_401('/api/rounds/%d/block.xlsx' % self.rid)

    def test_preview(self):
        self._must_be_401('/api/rounds/%d/preview' % self.rid)

    def test_pending_lists(self):
        self._must_be_401('/api/rounds/%d/pending-order' % self.rid)
        self._must_be_401('/api/rounds/%d/pending-invoice' % self.rid)

    def test_mail_reads(self):
        for path in ('/api/mail/presets', '/api/mail/config', '/api/mail/pending'):
            self._must_be_401(path)

    def test_mail_writes(self):
        self._must_be_401('/api/mail/test', method='post')
        self._must_be_401('/api/mail/fetch', method='post')
        self._must_be_401('/api/mail/config', method='put', json={})
        self._must_be_401('/api/mail/attach', method='post',
                          json={'mail_id': 1, 'item_id': 1})
        self._must_be_401('/api/mail/pending/1', method='delete')

    def test_member_management(self):
        self._must_be_401('/api/members', method='post', json={'name': '新同学'})
        self._must_be_401('/api/members/%d' % self.bob, method='put',
                          json={'name': '改个名'})
        self._must_be_401('/api/members/%d' % self.bob, method='delete')

    def test_round_management(self):
        self._must_be_401('/api/rounds', method='post', json={'name': '偷建的轮次'})
        self._must_be_401('/api/rounds/%d' % self.rid, method='put',
                          json={'name': '改名'})
        self._must_be_401('/api/rounds/%d/status' % self.rid, method='post',
                          json={'status': 'locked'})
        self._must_be_401('/api/rounds/%d/reorder' % self.rid, method='post',
                          json={'ids': []})

    def test_item_status(self):
        iid = self._add(self.purchase, self.bob)
        self._must_be_401('/api/items/%d/status' % iid, method='post',
                          json={'order_status': '已下单'})

    def test_unlocked_member_can_read(self):
        """解锁之后，组员要能选名字、看轮次、看条目和进度。"""
        for path in ('/api/meta', '/api/members', '/api/rounds',
                     '/api/rounds/%d' % self.rid,
                     '/api/rounds/%d/items' % self.rid,
                     '/api/rounds/%d/progress' % self.rid):
            self.assertEqual(self.alice_c.get(path).status_code, 200, path)

    def test_locked_client_sees_nothing(self):
        """**没解锁就什么都拿不到** —— 服务可能挂在公网上，这是底线。"""
        for path in ('/api/members', '/api/rounds',
                     '/api/rounds/%d' % self.rid,
                     '/api/rounds/%d/items' % self.rid,
                     '/api/rounds/%d/progress' % self.rid):
            self.assertEqual(self.anon.get(path).status_code, 401, path)

    def test_meta_is_the_only_open_read(self):
        """/api/meta 例外 —— 页面初始化要用它。"""
        self.assertEqual(self.anon.get('/api/meta').status_code, 200)


# ================================================================== 归属：自己 vs 别人

class TestOwnership(EntryCase):
    """组员只能动自己的（君子协定级，防手滑）；采购人员随便动。"""

    def test_member_cannot_touch_someone_elses_item(self):
        iid = self._add(self.bob_c, self.bob, '别人的东西')
        self.assertEqual(self.alice_c.put('/api/items/%d' % iid,
                                         json={'name': '我改'}).status_code, 403)
        self.assertEqual(self.alice_c.delete('/api/items/%d' % iid).status_code, 403)

    def test_member_can_edit_own_item(self):
        iid = self._add(self.alice_c, self.alice)
        r = self.alice_c.put('/api/items/%d' % iid, json={'name': '改过了'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['item']['name'], '改过了')

    def test_member_can_delete_own_item(self):
        iid = self._add(self.alice_c, self.alice)
        self.assertEqual(self.alice_c.delete('/api/items/%d' % iid).status_code, 200)

    def test_member_cannot_submit_under_someone_elses_name(self):
        r = self.alice_c.post('/api/rounds/%d/items' % self.rid,
                              json={'member_id': self.bob, 'name': '冒名顶替',
                                    'qty': 1, 'unit': '个', 'price': 5, 'purpose': '用'})
        self.assertEqual(r.status_code, 403)

    def test_purchase_can_edit_anyones_item(self):
        iid = self._add(self.alice_c, self.alice)
        r = self.purchase.put('/api/items/%d' % iid, json={'name': '采购帮着改'})
        self.assertEqual(r.status_code, 200)

    def test_locked_client_cannot_modify_anything(self):
        """没解锁的客户端连请求都发不出去（401），比 403 更靠前一道。"""
        iid = self._add(self.alice_c, self.alice)
        self.assertEqual(self.anon.put('/api/items/%d' % iid,
                                       json={'name': 'x'}).status_code, 401)
        self.assertEqual(self.anon.delete('/api/items/%d' % iid).status_code, 401)

    def test_invoice_upload_and_view_follow_ownership(self):
        mine = self._add(self.alice_c, self.alice)
        theirs = self._add(self.bob_c, self.bob)

        r = self.alice_c.post('/api/items/%d/invoices' % mine,
                              files={'file': ('x.pdf', PDF, 'application/pdf')})
        self.assertEqual(r.status_code, 200, r.text)
        inv = r.json()['invoice']['id']

        r = self.alice_c.post('/api/items/%d/invoices' % theirs,
                              files={'file': ('y.pdf', PDF, 'application/pdf')})
        self.assertEqual(r.status_code, 403, '不能往别人的条目上挂发票')

        # 看得见 —— 组员能看全组条目，发票算条目的一部分
        self.assertEqual(self.alice_c.get('/api/invoices/%d/file' % inv).status_code, 200)
        self.assertEqual(self.bob_c.get('/api/invoices/%d/file' % inv).status_code, 200)
        # 但删不掉
        self.assertEqual(self.bob_c.delete('/api/invoices/%d' % inv).status_code, 403)

    def test_purchase_can_upload_to_anyone(self):
        iid = self._add(self.alice_c, self.alice)
        r = self.purchase.post('/api/items/%d/invoices' % iid,
                               files={'file': ('z.pdf', PDF, 'application/pdf')})
        self.assertEqual(r.status_code, 200, r.text)


# ================================================================== 组员能看的进度

class TestProgress(EntryCase):

    def test_counts(self):
        self._add(self.alice_c, self.alice)
        self._add(self.bob_c, self.bob)
        p = self.alice_c.get('/api/rounds/%d/progress' % self.rid).json()
        self.assertEqual(p['total'], 2)
        self.assertEqual(p['ordered'], 0)
        self.assertEqual(p['need_order'], 2)
        self.assertEqual(p['need_invoice'], 2)

    def test_does_not_leak_other_peoples_items(self):
        self._add(self.bob_c, self.bob, '别人买的秘密东西')
        body = self.alice_c.get('/api/rounds/%d/progress' % self.rid).text
        self.assertNotIn('别人买的秘密东西', body)


if __name__ == '__main__':
    unittest.main(verbosity=2)


class TestFirstPasswordNeedsSetupCode(EntryCase):
    """**首次设密码必须带服务器上的初始化码**。

    这个路由要能匿名调用（不然死锁），但服务一旦暴露公网，
    路人也能抢先把密码设了、把采购权限拿走。初始化码只写在服务器上，
    所以「知道网址」不再等于「能夺权」。
    """

    def setUp(self):
        super().setUp()
        settings.set_password('')          # 回到「还没设过密码」
        settings.clear_setup_code()
        settings.reset_state()

    def test_without_the_code_it_is_refused(self):
        r = self.anon.post('/api/purchase/password', json={'new': 'abcd1234'})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(settings.has_password(), '没带码就不该把密码设上')

    def test_a_wrong_code_is_refused(self):
        settings.setup_code()              # 先让码存在
        r = self.anon.post('/api/purchase/password',
                           json={'new': 'abcd1234', 'setup': 'DEADBEEF'})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(settings.has_password())

    def test_the_right_code_sets_the_password(self):
        code = settings.setup_code()
        r = self.anon.post('/api/purchase/password',
                           json={'new': 'abcd1234', 'setup': code})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(settings.has_password())
        self.assertTrue(settings.check_password('abcd1234'))

    def test_the_code_is_deleted_after_use(self):
        """设好密码就把它删掉，别一直躺在磁盘上。"""
        code = settings.setup_code()
        self.assertTrue(settings.setup_code_file().exists())
        self.anon.post('/api/purchase/password', json={'new': 'abcd1234', 'setup': code})
        self.assertFalse(settings.setup_code_file().exists())

    def test_the_code_ignores_case_and_surrounding_spaces(self):
        code = settings.setup_code()
        r = self.anon.post('/api/purchase/password',
                           json={'new': 'abcd1234', 'setup': '  ' + code.lower() + '  '})
        self.assertEqual(r.status_code, 200)

    def test_an_old_deployment_without_a_code_file_can_still_set_one(self):
        """从老版本升级上来时没有码文件 —— 第一次请求要能给出一个码，
        否则永远设不了密码（但外人依然不知道它）。"""
        self.assertFalse(settings.setup_code_file().exists())
        r = self.anon.post('/api/purchase/password',
                           json={'new': 'abcd1234', 'setup': '猜测'})
        self.assertEqual(r.status_code, 403)
        self.assertTrue(settings.setup_code_file().exists(), '应当补生成一个码')

    def test_changing_an_existing_password_does_not_need_the_code(self):
        """已经设过密码时，改密码靠旧密码验证，和初始化码无关。"""
        settings.set_password(PW)
        # setUp 里 reset 过登录态，这里重新登录一次
        self.assertEqual(self.purchase.post('/api/purchase/login',
                                            json={'password': PW}).status_code, 200)
        r = self.purchase.post('/api/purchase/password',
                               json={'old': PW, 'new': 'abcd1234'})
        self.assertEqual(r.status_code, 200)


class TestSessionExpiryIsHandled(unittest.TestCase):
    """服务重启 → 内存里的登录凭证全没了。

    以前前端完全不管 401：所有保存都失败、界面还显示旧数据，
    人以为存上了其实什么都没写。现在必须**弹回登录门并停掉轮询**。
    """

    @classmethod
    def setUpClass(cls):
        cls.js = (Path(__file__).resolve().parent.parent / 'app/web/app.js').read_text(
            encoding='utf-8')

    def test_the_api_helper_reacts_to_401(self):
        seg = self.js.split('async function api(')[1].split('async function')[0]
        self.assertIn('res.status === 401', seg)
        self.assertIn('handleSessionExpired()', seg)

    def test_login_endpoints_are_excluded(self):
        """登录接口自己的 401 是「密码错」，不能当成会话过期去弹门。"""
        self.assertIn("'/api/purchase/login'", self.js)
        self.assertIn("'/api/member/login'", self.js)
        seg = self.js.split('function isAuthPath(')[1].split('\n}')[0]
        self.assertIn('AUTH_PATHS', seg)

    def test_it_stops_the_polling_and_shows_the_gate(self):
        seg = self.js.split('function handleSessionExpired(')[1].split('\n}')[0]
        self.assertIn('clearInterval(startAutoRefresh._t)', seg)
        self.assertIn('showLoginGate()', seg)
        self.assertIn('showMemberGate()', seg)

    def test_it_only_fires_once_per_burst(self):
        """一屏十几个请求同时 401，只该弹一次。"""
        seg = self.js.split('function handleSessionExpired(')[1].split('\n}')[0]
        self.assertIn('_done', seg)
        self.assertIn('return', seg)

    def test_a_new_login_rearms_it(self):
        self.assertIn('function resetSessionExpiredFlag(', self.js)
        # 两个登录成功处各一次（函数定义里没有这个带分号的调用形式）
        got = self.js.count('resetSessionExpiredFlag();')
        self.assertGreaterEqual(got, 2, '采购登录和组员口令两处都要复位')


class TestMemberCookieIsSigned(unittest.TestCase):
    """组员身份 cookie 必须**签过名**。

    以前 cookie 里就是明文 member id —— 把 `cgsys_member=7` 一改就成了别人，
    改条目、改实付、删发票全能做（`_guard_own_item` 完全绕开）。
    """

    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from app import config
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        for name, value in (('DATA_DIR', root),
                            ('ATTACH_DIR', root / 'attachments'),
                            ('INVOICE_DIR', root / 'invoices'),
                            ('MAIL_INBOX_DIR', root / 'mail_inbox'),
                            ('MAIL_CONFIG', root / 'mail.json'),
                            ('EXPORT_DIR', root / 'export'),
                            ('DB_PATH', root / 'procurement.db'),
                            ('ROSTER_FILE', root / 'roster.txt')):
            p = mock.patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_round_trip(self):
        got = settings.sign_member(7)
        self.assertEqual(settings.read_member(got), 7)

    def test_the_value_is_not_a_plain_number(self):
        self.assertNotEqual(settings.sign_member(7), '7')

    def test_a_tampered_id_is_rejected(self):
        """把 id 改大一位、签名不动 → 不认。"""
        value = settings.sign_member(7)
        raw, sig = value.rsplit('.', 1)
        self.assertIsNone(settings.read_member('8.' + sig))

    def test_a_tampered_signature_is_rejected(self):
        value = settings.sign_member(7)
        raw, sig = value.rsplit('.', 1)
        self.assertIsNone(settings.read_member(raw + '.' + 'f' * len(sig)))

    def test_the_old_plain_format_is_rejected(self):
        """升级前的老 cookie（纯数字）一律当作没选名字。"""
        self.assertIsNone(settings.read_member('7'))
        self.assertIsNone(settings.read_member(''))
        self.assertIsNone(settings.read_member(None))

    def test_the_secret_is_stable_across_calls(self):
        """密钥要落盘 —— 服务重启不能把所有人的身份弄丢。"""
        a = settings.member_secret()
        b = settings.member_secret()
        self.assertEqual(a, b)
        self.assertTrue(a)
