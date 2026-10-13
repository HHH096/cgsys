# -*- coding: utf-8 -*-
"""采购入口也要认名字。

登录只说明「你能管全组」，不说明「你是谁」——
采购自己上手买的那几条、垫的钱，同样得挂在具体人名下，
不然对账、垫资统计、导出的申购人全是糊的。
"""
import unittest
from pathlib import Path

from tests.support import EntryCase


class TestPurchaseNameGate(unittest.TestCase):
    """前端接线（这些是运行时行为，`node --check` 抓不到）。"""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.js = (root / 'app/web/app.js').read_text(encoding='utf-8')
        cls.html = (root / 'app/web/index.html').read_text(encoding='utf-8')
        cls.css = (root / 'app/web/styles.css').read_text(encoding='utf-8')

    def seg(self, name):
        return self.js.split(name)[1].split('\nfunction ')[0]

    # ------------------------------------------------------------ 两个入口都要过

    def test_purchase_entry_asks_who_you_are(self):
        seg = self.seg('async function startPurchase(')
        self.assertIn('ensureMyName()', seg)

    def test_the_first_time_login_path_asks_too(self):
        """新机器上第一条路是「设密码」，那条也得问。"""
        seg = self.seg('async function doLogin(')
        self.assertIn('ensureMyName()', seg)

    def test_both_entries_share_one_rule(self):
        self.assertIn('function ensureMyName(', self.js)
        seg = self.seg('function ensureMyName(')
        self.assertIn('restoreMyName()', seg)     # 认过就放行
        self.assertIn('showNameGate()', seg)      # 没认过就挡

    def test_a_name_that_was_not_picked_stays_empty(self):
        """下拉里排第一个 ≠ 他认了名字。

        要是 `state.me` 被预选的人顶上了，名字门就形同虚设：
        申购人、垫资人会悄悄记到第一个人头上。
        """
        seg = self.seg('async function loadMembers(')
        self.assertIn('const known', seg)
        self.assertIn('state.me = known ?', seg)

    # ------------------------------------------------------------ 采购的文案与出口

    def test_the_wording_is_different_for_purchase(self):
        seg = self.seg('function showNameGate(')
        self.assertIn("'你是哪位？'", seg)
        self.assertIn('采购也要认名字', seg)

    def test_the_gate_has_its_own_ids(self):
        for needle in ('id="nameGateTitle"', 'id="nameGateHint"'):
            self.assertIn(needle, self.html, needle)

    def test_the_manager_still_has_a_way_in(self):
        """名单空了、名字打错了 —— 管理入口不能把管理员自己锁在门外。"""
        self.assertIn('id="nameGateAdd"', self.html)
        tail = self.html.split('id="nameGateAdd"')[1].split('>')[0]
        self.assertIn('purchase-only', tail)               # 组员看不到这个出口
        self.assertIn('html.mode-member .purchase-only', self.css)
        self.assertIn('function openMemberAdmin(', self.js)
        self.assertIn("$('nameGateAdd').addEventListener('click', openMemberAdmin)", self.js)

    def test_that_way_in_opens_the_member_admin(self):
        seg = self.seg('function openMemberAdmin(')
        self.assertIn('addMemberRow()', seg)
        self.assertIn('memberCard', seg)
        self.assertIn('id="memberCard"', self.html)

    def test_adding_yourself_asks_for_the_name_again(self):
        seg = self.seg('async function saveMemberRow(')
        self.assertIn('if (!state.me) ensureMyName();', seg)

    # ------------------------------------------------------------ 「记住我是谁」

    def test_one_place_remembers_the_name(self):
        """以前名字门写 `cgsys_me`、顶部下拉写 `me_id`，各写各的 ——
        在下拉里选过名字的人，下次进来还是会被名字门拦一次。"""
        self.assertIn('function savedMeId(', self.js)
        self.assertIn('function rememberMeId(', self.js)
        self.assertIn('NAME_KEY', self.seg('function savedMeId('))
        remember = self.seg('function rememberMeId(')
        self.assertIn('NAME_KEY', remember)
        self.assertIn("removeItem('me_id')", remember)
        # 老数据还读得到
        self.assertIn("getItem('me_id')", self.seg('function savedMeId('))

    def test_picking_and_switching_both_remember(self):
        self.assertIn('rememberMeId(', self.seg('async function pickMyName('))
        seg = self.js.split("$('who').addEventListener('change'")[1].split('});')[0]
        self.assertIn('rememberMeId(', seg)

    def test_switching_a_name_goes_through_the_server(self):
        """换了名字要告诉服务端（条目归属、垫资、导出都看它）——
        采购也走同一条路，不再只是重画表格。"""
        seg = self.js.split("$('who').addEventListener('change'")[1].split('});')[0]
        self.assertIn('syncMeToServer()', seg)
        self.assertNotIn('if (!IS_PURCHASE)', seg)

    def test_picking_a_name_reloads_for_the_right_entry(self):
        seg = self.seg('async function pickMyName(')
        self.assertIn('IS_PURCHASE ? loadAllPurchase() : loadAllMember()', seg)
        self.assertIn('syncMeToServer()', seg)


class TestTheServerRecordsAnyoneWhoPicks(EntryCase):
    """/api/me 不分身份：采购也能把自己的名字记下来。"""

    def test_purchase_can_record_who_he_is(self):
        self.assertIsNone(self.purchase.get('/api/me').json()['member_id'])
        r = self.purchase.post('/api/me', json={'member_id': self.owner})
        self.assertEqual(r.status_code, 200, r.text)
        got = self.purchase.get('/api/me').json()
        self.assertEqual(got['member_id'], self.owner)
        self.assertTrue(got['is_purchase'])

    def test_a_member_still_records_his_own(self):
        self.alice_c.post('/api/me', json={'member_id': self.alice})
        self.assertEqual(self.alice_c.get('/api/me').json()['member_id'], self.alice)

    def test_an_unknown_name_is_refused(self):
        r = self.purchase.post('/api/me', json={'member_id': 999999})
        self.assertEqual(r.status_code, 404, r.text)

    def test_clearing_works(self):
        self.purchase.post('/api/me', json={'member_id': self.owner})
        self.purchase.post('/api/me', json={'member_id': None})
        self.assertIsNone(self.purchase.get('/api/me').json()['member_id'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
