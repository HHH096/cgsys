# -*- coding: utf-8 -*-
"""组员入口的共享口令，以及「没解锁一律 401」这条底线。

这条底线存在的理由：服务随时可能通过隧道发布到公网，
那时组员入口如果没有口令，就等于把全组的采购数据摆在网上。
"""
import unittest

from app.core import settings
from tests.support import TEST_MEMBER_PW, EntryCase, TempDataTest


class TestMemberPasswordStore(TempDataTest):
    """口令本身怎么存。"""

    def test_nothing_set_initially(self):
        self.assertFalse(settings.has_member_password())
        self.assertFalse(settings.check_member_password(''))

    def test_set_then_check(self):
        settings.set_member_password('dianqi-2026')
        self.assertTrue(settings.has_member_password())
        self.assertTrue(settings.check_member_password('dianqi-2026'))
        self.assertFalse(settings.check_member_password('不对'))

    def test_not_plaintext_on_disk(self):
        pw = 'dianqi-2026'
        settings.set_member_password(pw)
        raw = settings.settings_file().read_text(encoding='utf-8')
        self.assertNotIn(pw, raw, '口令不该明文躺在配置文件里')

    def test_chinese_password_works(self):
        """密码可以是中文（compare_digest 只吃 ASCII，所以要编码成 bytes）。"""
        settings.set_member_password('电气组口令')
        self.assertTrue(settings.check_member_password('电气组口令'))
        self.assertFalse(settings.check_member_password('电气组'))

    def test_the_two_passwords_are_separate(self):
        """组员口令和采购密码是两个字段，别串了。"""
        settings.set_password('purchase-one')
        settings.set_member_password('member-one')
        self.assertTrue(settings.check_password('purchase-one'))
        self.assertFalse(settings.check_password('member-one'))
        self.assertTrue(settings.check_member_password('member-one'))
        self.assertFalse(settings.check_member_password('purchase-one'))

    def test_empty_closes_the_entry(self):
        settings.set_member_password('x')
        settings.set_member_password('')
        self.assertFalse(settings.has_member_password())


class TestTokenKinds(TempDataTest):
    """两种身份不能混用。"""

    def test_member_token_is_not_a_purchase_token(self):
        t = settings.issue_token(settings.KIND_MEMBER)
        self.assertTrue(settings.verify_token(t, settings.KIND_MEMBER))
        self.assertFalse(settings.verify_token(t, settings.KIND_PURCHASE))

    def test_purchase_token_is_not_a_member_token(self):
        t = settings.issue_token(settings.KIND_PURCHASE)
        self.assertTrue(settings.verify_token(t, settings.KIND_PURCHASE))
        self.assertFalse(settings.verify_token(t, settings.KIND_MEMBER))


class TestMemberGate(EntryCase):
    """口令门的行为。"""

    def test_status_before_unlocking(self):
        st = self.anon.get('/api/member/status').json()
        self.assertTrue(st['has_password'])
        self.assertFalse(st['unlocked'])

    def test_status_after_unlocking(self):
        st = self.alice_c.get('/api/member/status').json()
        self.assertTrue(st['has_password'])
        self.assertTrue(st['unlocked'])

    def test_no_password_means_closed(self):
        settings.set_member_password('')
        st = self.anon.get('/api/member/status').json()
        self.assertFalse(st['has_password'])
        self.assertFalse(st['unlocked'], '没设口令时组员入口应当是关着的')

    def test_wrong_password(self):
        r = self.anon.post('/api/member/login', json={'password': '不对'})
        self.assertEqual(r.status_code, 401)

    def test_login_before_a_password_exists(self):
        settings.set_member_password('')
        r = self.anon.post('/api/member/login', json={'password': '随便'})
        self.assertEqual(r.status_code, 400, '要说清楚「还没设过口令」')

    def test_login_then_can_read(self):
        self.assertEqual(self.anon.get('/api/rounds').status_code, 401)
        r = self.anon.post('/api/member/login', json={'password': TEST_MEMBER_PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.anon.get('/api/rounds').status_code, 200)

    def test_lockout_on_repeated_wrong_passwords(self):
        for _ in range(settings.MAX_FAILS):
            self.anon.post('/api/member/login', json={'password': 'x'})
        r = self.anon.post('/api/member/login', json={'password': TEST_MEMBER_PW})
        self.assertEqual(r.status_code, 429, '锁定之后即使口令对也得先等着')

    def test_purchase_login_also_opens_it(self):
        """采购人员本来就能看全部，不该再被组员口令挡一道。"""
        self.assertTrue(self.purchase.get('/api/member/status').json()['unlocked'])
        self.assertEqual(self.purchase.get('/api/rounds').status_code, 200)

    def test_a_member_token_does_not_open_the_purchase_entry(self):
        """反过来也要成立：组员口令换来的身份，进不了采购入口。"""
        r = self.alice_c.get('/api/rounds/%d/pending-order' % self.rid)
        self.assertEqual(r.status_code, 401)


class TestSettingTheMemberPassword(EntryCase):
    """设口令这件事，只有采购人员能做。"""

    def test_needs_purchase_login(self):
        for label, client in (('匿名', self.anon), ('组员', self.alice_c)):
            r = client.post('/api/purchase/member-password',
                            json={'password': 'newpass'})
            self.assertEqual(r.status_code, 401, '%s 不该能改组员口令' % label)

    def test_purchase_can_set_it(self):
        r = self.purchase.post('/api/purchase/member-password',
                               json={'password': 'xindekouling'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['has_password'])
        self.assertTrue(settings.check_member_password('xindekouling'))

    def test_too_short_is_rejected(self):
        r = self.purchase.post('/api/purchase/member-password',
                               json={'password': '12'})
        self.assertEqual(r.status_code, 400)

    def test_empty_closes_the_entry(self):
        r = self.purchase.post('/api/purchase/member-password', json={'password': ''})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['has_password'])

    def test_changing_it_kicks_the_members_out(self):
        """改口令 / 关口令之后，老组员手里的 cookie 立刻失效。

        以前是「不踢人」—— 用起来省事，但等于**关了门钥匙还能开 30 天**：
        口令本来就发在群里，改掉之后老口令仍然有效的那段时间是真实的风险窗口。
        采购自己的登录不受影响（吊销按身份分类，见 settings.revoke_kind）。
        """
        r = self.purchase.post('/api/purchase/member-password',
                               json={'password': 'brand-new-pw'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertGreater(r.json().get('revoked', 0), 0, '应当吊销了老令牌')
        self.assertEqual(self.alice_c.get('/api/rounds').status_code, 401,
                         '老 cookie 必须失效')
        self.assertEqual(self.purchase.get('/api/rounds').status_code, 200,
                         '采购自己不该被踢')


class TestTheFirstRunIsNotLockedOut(EntryCase):
    """**这条是给「全新安装」兜底的。**

    曾经把 /api/purchase/password 漏出白名单，结果：
    进不去 → 设不了密码 → 设不了密码 → 更进不去，直接死锁。
    """

    def test_purchase_password_can_be_set_before_any_login(self):
        settings.reset_state()                 # 清掉所有登录
        settings.set_password('')
        settings.clear_setup_code()
        fresh = self._client()
        # 初始化码只写在服务器上（这里从 settings 取，等价于 SSH 上去看那个文件）
        r = fresh.post('/api/purchase/password',
                       json={'new': 'brand-new', 'setup': settings.setup_code()})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(fresh.get('/api/purchase/status').json()['logged_in'])

    def test_a_stranger_without_the_code_cannot_claim_the_purchase_entry(self):
        """**知道网址不等于能夺权** —— 没有服务器上的初始化码就设不了密码。

        这条和上一条是一对：既要「全新安装不会死锁」（能设），
        又要「公网路人抢不了」（别人设不了）。
        """
        settings.reset_state()
        settings.set_password('')
        settings.clear_setup_code()
        fresh = self._client()
        r = fresh.post('/api/purchase/password', json={'new': 'brand-new'})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertFalse(settings.has_password(), '密码不该被路人设上')

    def test_meta_is_readable_while_locked(self):
        r = self.anon.get('/api/meta')
        self.assertEqual(r.status_code, 200, '页面初始化要用它')


if __name__ == '__main__':
    unittest.main(verbosity=2)
